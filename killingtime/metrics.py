"""Derived progression metrics. Every function returns plain dicts/lists so the results can be
rendered by the web UI, printed by the CLI, or handed to Claude as tool output.

Most functions take an optional ``team`` (a raid team name from ``RAID_TEAMS``). With a team the numbers come
from that team's reports only (``v_team_first_kills`` / ``v_team_raid_nights``); without one they cover the
whole guild."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import UTC, datetime
from typing import Any

from .db import CODE_TO_RIO_DIFFICULTY, DIFFICULTIES

DAY_MS = 86_400_000
RAID_DIFFS = (5, 4, 3)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def ms_to_date(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _scope(team: str | None) -> tuple[str, str, str, tuple]:
    """(first-kills view, raid-nights view, extra ``AND team = ?`` clause, its params) for a team or the whole guild."""
    if team:
        return "v_team_first_kills", "v_team_raid_nights", " AND team = ?", (team,)
    return "v_first_kills", "v_raid_nights", "", ()


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 1) if values else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 1)


def home_guild(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM guilds WHERE is_home = 1 LIMIT 1").fetchone()
    return dict(row) if row else None


def teams_seen(conn: sqlite3.Connection) -> list[str]:
    """Raid teams that have at least one report assigned (in roster order of first appearance)."""
    return [r["team"] for r in conn.execute("SELECT team FROM report_teams GROUP BY team ORDER BY MIN(rowid)")]


# ------------------------------------------------------------------------------------------ tiers
def tiers(conn: sqlite3.Connection, team: str | None = None) -> list[dict[str, Any]]:
    """Zones that have any of our pulls, newest first, with a per-difficulty kill count."""
    fk, _, tf, tp = _scope(team)
    team_join = "JOIN report_teams t ON t.report_code = r.code AND t.team = ?" if team else ""
    rows = _rows(
        conn,
        f"""
        SELECT z.id, z.name, z.frozen, z.rio_raid_slug, x.name AS expansion,
               (SELECT COUNT(*) FROM encounters e WHERE e.zone_id = z.id) AS bosses,
               (SELECT COUNT(*) FROM reports r {team_join} WHERE r.zone_id = z.id) AS reports,
               (SELECT MIN(start_time) FROM reports r {team_join} WHERE r.zone_id = z.id) AS first_report,
               (SELECT MAX(end_time) FROM reports r {team_join} WHERE r.zone_id = z.id) AS last_report
        FROM zones z LEFT JOIN expansions x ON x.id = z.expansion_id
        WHERE EXISTS (SELECT 1 FROM reports r {team_join} WHERE r.zone_id = z.id)
        ORDER BY z.id DESC
        """,
        tp * 4,
    )
    for r in rows:
        kills = _rows(
            conn,
            f"SELECT difficulty, SUM(killed) AS killed FROM {fk} WHERE zone_id = ?{tf} GROUP BY difficulty",
            (r["id"], *tp),
        )
        r["kills"] = {int(k["difficulty"]): int(k["killed"] or 0) for k in kills if k["difficulty"] is not None}
        r["first_report_date"] = ms_to_date(r["first_report"])
        r["last_report_date"] = ms_to_date(r["last_report"])
        r["summary"] = " / ".join(
            f"{r['kills'].get(d, 0)}/{r['bosses']} {DIFFICULTIES[d][0]}" for d in RAID_DIFFS if d in r["kills"]
        )
    return rows


def current_tier(conn: sqlite3.Connection, team: str | None = None) -> dict[str, Any] | None:
    t = tiers(conn, team)
    return t[0] if t else None


def best_difficulty(conn: sqlite3.Connection, zone_id: int, team: str | None = None) -> int:
    _, _, tf, tp = _scope(team)
    row = conn.execute(
        f"SELECT MAX(difficulty) AS d FROM v_pulls WHERE zone_id = ? AND difficulty IN (3,4,5){tf}", (zone_id, *tp)
    ).fetchone()
    return int(row["d"]) if row and row["d"] else 5


def tier_summary(conn: sqlite3.Connection, zone_id: int, difficulty: int, team: str | None = None) -> dict[str, Any]:
    fk, _, tf, tp = _scope(team)
    fk_team = " AND fk.team = ?" if team else ""
    zone = conn.execute("SELECT * FROM zones WHERE id = ?", (zone_id,)).fetchone()
    bosses = _rows(
        conn,
        f"""
        SELECT e.id, e.name, e.ord, e.rio_encounter_slug, fk.killed, fk.first_kill_time, fk.first_kill_date, fk.pulls_to_kill,
               fk.wipes_before_kill, fk.best_wipe_pct, fk.nights_to_kill, fk.hours_to_kill, fk.first_pull_time,
               (SELECT COUNT(*) FROM v_pulls p WHERE p.encounter_id = e.id AND p.difficulty = ?{tf}) AS total_pulls,
               (SELECT SUM(kill) FROM v_pulls p WHERE p.encounter_id = e.id AND p.difficulty = ?{tf}) AS total_kills,
               (SELECT MIN(fight_pct) FROM v_pulls p WHERE p.encounter_id = e.id AND p.difficulty = ?{tf} AND kill = 0) AS best_pct
        FROM encounters e
        LEFT JOIN {fk} fk ON fk.encounter_id = e.id AND fk.difficulty = ?{fk_team}
        WHERE e.zone_id = ?
        ORDER BY e.ord
        """,
        (difficulty, *tp, difficulty, *tp, difficulty, *tp, difficulty, *tp, zone_id),
    )
    totals = conn.execute(
        f"""SELECT COUNT(*) AS pulls, SUM(kill) AS kills, COUNT(DISTINCT pull_date) AS nights,
                   SUM(duration_s)/3600.0 AS hours, MIN(start_time) AS first_pull, MAX(end_time) AS last_pull,
                   AVG(avg_ilvl) AS avg_ilvl
            FROM v_pulls WHERE zone_id = ? AND difficulty = ?{tf}""",
        (zone_id, difficulty, *tp),
    ).fetchone()
    killed = sum(1 for b in bosses if b["killed"])
    first_pull = totals["first_pull"]
    last_kill = max((b["first_kill_time"] for b in bosses if b["first_kill_time"]), default=None)
    days_to_current = (last_kill - first_pull) / DAY_MS if (first_pull and last_kill) else None
    return {
        "zone": dict(zone) if zone else None,
        "team": team,
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
        "last_kill_date": ms_to_date(last_kill),
        "days_to_latest_kill": round(days_to_current, 1) if days_to_current is not None else None,
        "next_boss": next((b for b in bosses if not b["killed"]), None),
    }


def progress_timeline(conn: sqlite3.Connection, zone_id: int, difficulty: int, team: str | None = None) -> list[dict[str, Any]]:
    """Cumulative bosses killed by day, plus day index since our first pull in this zone/difficulty."""
    fk, _, tf, tp = _scope(team)
    first = conn.execute(
        f"SELECT MIN(start_time) AS t FROM v_pulls WHERE zone_id = ? AND difficulty = ?{tf}", (zone_id, difficulty, *tp)
    ).fetchone()["t"]
    kills = _rows(
        conn,
        f"""SELECT encounter_name, first_kill_time FROM {fk}
            WHERE zone_id = ? AND difficulty = ? AND killed = 1{tf} ORDER BY first_kill_time""",
        (zone_id, difficulty, *tp),
    )
    out = []
    for i, k in enumerate(kills, start=1):
        out.append(
            {
                "boss": k["encounter_name"],
                "date": ms_to_date(k["first_kill_time"]),
                "day": round((k["first_kill_time"] - first) / DAY_MS, 1) if first else None,
                "kills": i,
            }
        )
    return out


def tier_comparison(conn: sqlite3.Connection, difficulty: int, team: str | None = None) -> list[dict[str, Any]]:
    """Per zone: cumulative pulls and days to each boss kill in boss order, for tier-over-tier comparison."""
    out = []
    for t in tiers(conn, team):
        s = tier_summary(conn, t["id"], difficulty, team)
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
                    "days_to_kill": round((b["first_kill_time"] - first_pull) / DAY_MS, 1)
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


def raid_nights(conn: sqlite3.Connection, zone_id: int, limit: int = 60, team: str | None = None) -> list[dict[str, Any]]:
    _, rn, tf, tp = _scope(team)
    n_team = " AND n.team = ?" if team else ""
    p_team = " AND p.team = n.team" if team else ""
    return _rows(
        conn,
        f"""SELECT pull_date, difficulty, pulls, kills, wipes, bosses_pulled, ROUND(hours_in_combat, 2) AS hours_in_combat,
                   ROUND(avg_ilvl, 1) AS avg_ilvl,
                   (SELECT GROUP_CONCAT(DISTINCT encounter_name) FROM v_pulls p
                     WHERE p.zone_id = n.zone_id AND p.difficulty = n.difficulty AND p.pull_date = n.pull_date AND p.kill = 1{p_team}) AS killed_bosses
            FROM {rn} n WHERE zone_id = ?{n_team} ORDER BY pull_date DESC, difficulty DESC LIMIT ?""",
        (zone_id, *tp, limit),
    )


def attendance_summary(conn: sqlite3.Connection, zone_id: int, team: str | None = None) -> dict[str, Any]:
    _, _, tf, tp = _scope(team)
    # Raids are counted by date: two people logging the same night produce two reports of one raid.
    total_raids = conn.execute(
        f"SELECT COUNT(DISTINCT raid_date) AS c FROM v_attendance WHERE zone_id = ?{tf}", (zone_id, *tp)
    ).fetchone()["c"]
    players = _rows(
        conn,
        f"""SELECT player_name, player_class, COUNT(DISTINCT raid_date) AS raids,
                   ROUND(100.0 * COUNT(DISTINCT raid_date) / ?, 1) AS pct
            FROM v_attendance WHERE zone_id = ? AND presence = 1{tf}
            GROUP BY player_name ORDER BY raids DESC, player_name""",
        (max(total_raids, 1), zone_id, *tp),
    )
    return {"total_raids": total_raids, "players": players}


def latest_kills(conn: sqlite3.Connection, limit: int = 10, team: str | None = None, zone_id: int | None = None) -> list[dict[str, Any]]:
    """Most recent first kills (Normal/Heroic/Mythic), newest first, with the team that got each kill."""
    fk, _, tf, tp = _scope(team)
    zf = " AND fk.zone_id = ?" if zone_id else ""
    zp = (zone_id,) if zone_id else ()
    team_col = "fk.team" if team else (
        "(SELECT p.team FROM v_pulls p WHERE p.encounter_id = fk.encounter_id AND p.difficulty = fk.difficulty "
        "AND p.kill = 1 AND p.start_time = fk.first_kill_time LIMIT 1)"
    )
    rows = _rows(
        conn,
        f"""SELECT fk.zone_id, fk.zone_name, fk.encounter_name AS boss, fk.encounter_ord, fk.difficulty, fk.first_kill_time,
                   fk.first_kill_date AS date, fk.pulls_to_kill AS pulls, fk.nights_to_kill AS nights, {team_col} AS team
            FROM {fk} fk WHERE fk.killed = 1 AND fk.difficulty IN (3,4,5){tf.replace('team', 'fk.team')}{zf}
            ORDER BY fk.first_kill_time DESC LIMIT ?""",
        (*tp, *zp, limit),
    )
    for r in rows:
        r["difficulty_name"] = DIFFICULTIES.get(r["difficulty"], str(r["difficulty"]))
    return rows


def team_progress(conn: sqlite3.Connection, zone_id: int, teams: list[str]) -> list[dict[str, Any]]:
    """Per team: kills per difficulty in a zone, the boss being worked on and the latest kill. Used by the dashboard and the public site."""
    out = []
    for team in teams:
        diffs = []
        for d in RAID_DIFFS:
            s = tier_summary(conn, zone_id, d, team)
            if s["pulls"] == 0:
                continue
            diffs.append(
                {
                    "difficulty": d,
                    "name": DIFFICULTIES[d],
                    "killed": s["killed"],
                    "total": s["total_bosses"],
                    "pulls": s["pulls"],
                    "nights": s["nights"],
                    "cleared": s["cleared"],
                    "next_boss": s["next_boss"]["name"] if s["next_boss"] else None,
                    "next_best_pct": s["next_boss"]["best_pct"] if s["next_boss"] else None,
                    "last_kill_date": s["last_kill_date"],
                    "first_pull_date": s["first_pull_date"],
                }
            )
        out.append({"team": team, "difficulties": diffs, "latest_kills": latest_kills(conn, 5, team=team, zone_id=zone_id)})
    return out


# ------------------------------------------------------------------------------------------ Raider.IO
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


# ------------------------------------------------------------------------------------------ peers
def _our_boss_progress(conn: sqlite3.Connection, raid_slug: str, difficulty: int, team: str | None) -> dict[str, dict[str, Any]]:
    """Our per-boss progress keyed by Raider.IO boss slug: from our logs when the zone is mapped, else Raider.IO."""
    zone = conn.execute("SELECT id FROM zones WHERE rio_raid_slug = ?", (raid_slug,)).fetchone()
    out: dict[str, dict[str, Any]] = {}
    if zone:
        fk, _, tf, tp = _scope(team)
        rows = _rows(
            conn,
            f"""SELECT e.rio_encounter_slug AS slug, fk.killed, fk.pulls_to_kill, fk.first_kill_time, fk.first_pull_time
                FROM encounters e LEFT JOIN {fk} fk ON fk.encounter_id = e.id AND fk.difficulty = ?{tf.replace('team', 'fk.team')}
                WHERE e.zone_id = ? AND e.rio_encounter_slug IS NOT NULL""",
            (difficulty, *tp, zone["id"]),
        )
        for r in rows:
            out[r["slug"]] = {
                "killed": bool(r["killed"]),
                "pulls": r["pulls_to_kill"] if r["pulls_to_kill"] else None,
                "first_kill_ms": r["first_kill_time"],
                "started_ms": r["first_pull_time"],
                "source": "logs",
            }
    if out and any(v["pulls"] for v in out.values()):
        return out
    if team:  # a team without logs in this raid has no progress of its own
        return out
    home = home_guild(conn)
    if not home:
        return out
    for p in _rows(conn, "SELECT * FROM rio_progress WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?", (home["id"], raid_slug, difficulty)):
        out[p["encounter_slug"]] = {
            "killed": bool(p["is_defeated"]),
            "pulls": p["num_pulls"] or None,
            "first_kill_ms": p["first_defeated"],
            "started_ms": p["pull_started_at"] or p["first_defeated"],
            "source": "raider.io",
        }
    return out


def peer_comparison(
    conn: sqlite3.Connection, raid_slug: str, difficulty: int, team: str | None = None, min_peers: int = 8, max_peers: int = 40
) -> dict[str, Any]:
    """Compare us with guilds at a similar point in the raid ("around our level"), not the realm's top.

    Peers are guilds (realm leaderboard + rivals) whose kill count is within a band of ours; the band widens until
    at least ``min_peers`` qualify. Per boss: our pulls vs the peers' average/median/quartiles, the share of peers we
    out-pulled (percentile), and days from the guild's first pull in the raid to the kill vs the peers' typical."""
    bosses = _rows(conn, "SELECT slug, name, ord FROM rio_encounters WHERE raid_slug = ? ORDER BY ord", (raid_slug,))
    home = home_guild(conn)
    home_id = home["id"] if home else -1
    ours = _our_boss_progress(conn, raid_slug, difficulty, team)
    our_kills = sum(1 for b in bosses if ours.get(b["slug"], {}).get("killed"))
    our_start = min((v["started_ms"] for v in ours.values() if v.get("started_ms")), default=None)

    guild_rows = _rows(
        conn,
        """SELECT g.id, g.name, g.realm_slug, g.is_rival, rk.realm_rank, rk.world_rank
           FROM guilds g LEFT JOIN rio_rankings rk ON rk.guild_id = g.id AND rk.raid_slug = ? AND rk.difficulty = ?
           WHERE g.is_home = 0 AND EXISTS (SELECT 1 FROM rio_progress p WHERE p.guild_id = g.id AND p.raid_slug = ? AND p.difficulty = ?)""",
        (raid_slug, difficulty, raid_slug, difficulty),
    )
    prog_rows = _rows(
        conn,
        "SELECT guild_id, encounter_slug, is_defeated, num_pulls, first_defeated, pull_started_at FROM rio_progress WHERE raid_slug = ? AND difficulty = ? AND guild_id != ?",
        (raid_slug, difficulty, home_id),
    )
    by_guild: dict[int, dict[str, dict]] = {}
    for p in prog_rows:
        by_guild.setdefault(p["guild_id"], {})[p["encounter_slug"]] = p
    candidates = []
    for g in guild_rows:
        prog = by_guild.get(g["id"], {})
        killed = sum(1 for p in prog.values() if p["is_defeated"])
        starts = [p["pull_started_at"] or p["first_defeated"] for p in prog.values() if (p["pull_started_at"] or p["first_defeated"])]
        candidates.append({**g, "killed": killed, "prog": prog, "start_ms": min(starts) if starts else None,
                           "has_pulls": any((p["num_pulls"] or 0) > 0 for p in prog.values())})
    band = 0
    peers: list[dict] = []
    while band <= len(bosses):
        peers = [c for c in candidates if abs(c["killed"] - our_kills) <= band and c["killed"] > 0]
        if len(peers) >= min_peers:
            break
        band += 1
    # Closest kill counts first, then the guilds ranked nearest to us. Our own rank only describes the team when the
    # team's progress is the guild's (Raider.IO knows guilds, not teams); otherwise use the middle of the band.
    home_prog = by_guild_home = {p["encounter_slug"]: p for p in _rows(
        conn, "SELECT encounter_slug, is_defeated FROM rio_progress WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?",
        (home_id, raid_slug, difficulty))}
    guild_kills = sum(1 for p in by_guild_home.values() if p["is_defeated"])
    home_rank = conn.execute(
        "SELECT realm_rank FROM rio_rankings WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?", (home_id, raid_slug, difficulty)
    ).fetchone()
    ranks = sorted(c["realm_rank"] for c in peers if c["realm_rank"])
    if home_rank and home_rank["realm_rank"] and (our_kills == guild_kills or not home_prog):
        reference = home_rank["realm_rank"]
    else:
        reference = ranks[len(ranks) // 2] if ranks else 0
    peers.sort(key=lambda c: (abs(c["killed"] - our_kills), abs((c["realm_rank"] or 10**6) - reference)))
    peers = peers[:max_peers]

    per_boss = []
    for b in bosses:
        slug = b["slug"]
        mine = ours.get(slug, {})
        peer_pulls = [c["prog"][slug]["num_pulls"] for c in peers if slug in c["prog"] and c["prog"][slug]["is_defeated"] and (c["prog"][slug]["num_pulls"] or 0) > 0]
        peer_days = [
            (c["prog"][slug]["first_defeated"] - c["start_ms"]) / DAY_MS
            for c in peers
            if slug in c["prog"] and c["prog"][slug]["is_defeated"] and c["prog"][slug]["first_defeated"] and c["start_ms"]
        ]
        killed_by = sum(1 for c in peers if slug in c["prog"] and c["prog"][slug]["is_defeated"])
        our_pulls = mine.get("pulls")
        beat = None
        if our_pulls and peer_pulls:
            beat = round(100 * sum(1 for p in peer_pulls if p > our_pulls) / len(peer_pulls))
        our_days = None
        if mine.get("killed") and mine.get("first_kill_ms") and our_start:
            our_days = round((mine["first_kill_ms"] - our_start) / DAY_MS, 1)
        per_boss.append(
            {
                "ord": b["ord"],
                "boss": b["name"],
                "slug": slug,
                "killed": bool(mine.get("killed")),
                "our_pulls": our_pulls,
                "our_first_kill": ms_to_date(mine.get("first_kill_ms")),
                "our_days": our_days,
                "peers_killed": killed_by,
                "peers_with_pulls": len(peer_pulls),
                "peer_mean_pulls": _mean(peer_pulls),
                "peer_median_pulls": _median(peer_pulls),
                "peer_p25_pulls": _quantile(peer_pulls, 0.25),
                "peer_p75_pulls": _quantile(peer_pulls, 0.75),
                "beat_pct": beat,  # % of peers who needed more pulls than us
                "peer_median_days": _median(peer_days),
                "peer_p25_days": _quantile(peer_days, 0.25),
                "peer_p75_days": _quantile(peer_days, 0.75),
            }
        )
    killed_slugs = [b["slug"] for b in per_boss if b["killed"] and b["our_pulls"]]
    our_total = sum(b["our_pulls"] for b in per_boss if b["slug"] in killed_slugs)
    peer_totals = []
    for c in peers:
        if c["has_pulls"] and all(s in c["prog"] and c["prog"][s]["is_defeated"] and (c["prog"][s]["num_pulls"] or 0) > 0 for s in killed_slugs):
            peer_totals.append(sum(c["prog"][s]["num_pulls"] for s in killed_slugs))
    overall_beat = round(100 * sum(1 for t in peer_totals if t > our_total) / len(peer_totals)) if (peer_totals and our_total) else None
    return {
        "raid_slug": raid_slug,
        "difficulty": difficulty,
        "difficulty_name": DIFFICULTIES.get(difficulty),
        "team": team,
        "source": next((v["source"] for v in ours.values()), None),
        "our_kills": our_kills,
        "total_bosses": len(bosses),
        "band": band,
        "peer_count": len(peers),
        "peer_kills_median": _median([c["killed"] for c in peers]),
        "our_total_pulls": our_total or None,
        "peer_median_total_pulls": _median(peer_totals),
        "overall_beat_pct": overall_beat,
        "bosses": per_boss,
        "peers": [
            {"name": c["name"], "realm": c["realm_slug"], "killed": c["killed"], "realm_rank": c["realm_rank"], "world_rank": c["world_rank"],
             "is_rival": bool(c["is_rival"]), "pulls": sum((p["num_pulls"] or 0) for p in c["prog"].values()) or None}
            for c in peers
        ],
    }


# ------------------------------------------------------------------------------------------ parses
def performance(
    conn: sqlite3.Connection, zone_id: int, difficulty: int | None = None, team: str | None = None, include_pugs: bool = False
) -> dict[str, Any]:
    """Parse summary for a tier: per player (avg/median/best rank percentile), per boss and per raid night."""
    home = home_guild(conn)
    realm = (home["realm_slug"] if home else "").replace("-", " ")
    where = ["zone_id = ?", "difficulty IN (3,4,5)"]
    params: list[Any] = [zone_id]
    if difficulty:
        where.append("difficulty = ?")
        params.append(difficulty)
    if team:
        where.append("team = ?")
        params.append(team)
    if not include_pugs and realm:
        where.append("(server IS NULL OR LOWER(REPLACE(server, '''', '')) = ?)")
        params.append(realm.lower())
    w = " AND ".join(where)
    rows = _rows(
        conn,
        f"SELECT player_name, player_class, spec, role, metric, rank_percent, bracket_percent, amount, encounter_id, encounter_name, encounter_ord, kill_date, difficulty FROM v_parses WHERE {w}",
        tuple(params),
    )
    players: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["rank_percent"] is None:
            continue
        p = players.setdefault(r["player_name"], {"player": r["player_name"], "class": r["player_class"], "specs": {}, "roles": {}, "parses": [], "brackets": []})
        p["specs"][r["spec"]] = p["specs"].get(r["spec"], 0) + 1
        p["roles"][r["role"]] = p["roles"].get(r["role"], 0) + 1
        p["parses"].append(r["rank_percent"])
        if r["bracket_percent"] is not None:
            p["brackets"].append(r["bracket_percent"])
    player_rows = []
    for p in players.values():
        player_rows.append(
            {
                "player": p["player"],
                "class": p["class"],
                "spec": max(p["specs"], key=p["specs"].get) if p["specs"] else None,
                "role": max(p["roles"], key=p["roles"].get) if p["roles"] else None,
                "kills": len(p["parses"]),
                "avg": _mean(p["parses"]),
                "median": _median(p["parses"]),
                "best": round(max(p["parses"]), 1),
                "avg_bracket": _mean(p["brackets"]),
            }
        )
    player_rows.sort(key=lambda r: (-(r["avg"] or 0), r["player"]))
    by_boss: dict[int, dict[str, Any]] = {}
    for r in rows:
        if r["rank_percent"] is None:
            continue
        b = by_boss.setdefault(r["encounter_id"], {"boss": r["encounter_name"], "ord": r["encounter_ord"], "parses": [], "kills": set(), "best": None})
        b["parses"].append(r["rank_percent"])
        b["kills"].add((r["kill_date"], r["difficulty"]))
        if b["best"] is None or r["rank_percent"] > b["best"][1]:
            b["best"] = (r["player_name"], r["rank_percent"])
    boss_rows = sorted(
        (
            {"boss": b["boss"], "ord": b["ord"], "kills": len(b["kills"]), "parses": len(b["parses"]), "median": _median(b["parses"]),
             "avg": _mean(b["parses"]), "best_player": b["best"][0] if b["best"] else None, "best": b["best"][1] if b["best"] else None}
            for b in by_boss.values()
        ),
        key=lambda b: b["ord"],
    )
    by_night: dict[str, list[float]] = {}
    for r in rows:
        if r["rank_percent"] is not None and r["kill_date"]:
            by_night.setdefault(r["kill_date"], []).append(r["rank_percent"])
    nights = [{"date": d, "median": _median(v), "parses": len(v)} for d, v in sorted(by_night.items())]
    all_parses = [r["rank_percent"] for r in rows if r["rank_percent"] is not None]
    return {
        "zone_id": zone_id,
        "difficulty": difficulty,
        "team": team,
        "include_pugs": include_pugs,
        "players": player_rows,
        "bosses": boss_rows,
        "nights": nights,
        "parses": len(all_parses),
        "median": _median(all_parses),
        "avg": _mean(all_parses),
        "kills": len({(r["encounter_id"], r["kill_date"], r["difficulty"]) for r in rows}),
    }


def parse_coverage(conn: sqlite3.Connection, zone_id: int) -> dict[str, int]:
    """How many kill reports in a zone have parses fetched, for the Status page."""
    row = conn.execute(
        """SELECT COUNT(*) AS reports,
                  SUM(CASE WHEN rankings_synced_at IS NOT NULL THEN 1 ELSE 0 END) AS synced,
                  (SELECT COUNT(*) FROM v_parses p WHERE p.zone_id = ?) AS parses
           FROM reports WHERE zone_id = ?""",
        (zone_id, zone_id),
    ).fetchone()
    return {"reports": row["reports"] or 0, "synced": row["synced"] or 0, "parses": row["parses"] or 0}


def roster(conn: sqlite3.Connection, zone_id: int, difficulty: int | None, team: str | None, include_pugs: bool = False) -> dict[str, Any]:
    """One row per raider for a tier: attendance (all difficulties) merged with parses (selected difficulty)."""
    att = attendance_summary(conn, zone_id, team)
    perf = performance(conn, zone_id, difficulty, team, include_pugs=include_pugs)
    by_player = {p["player"]: p for p in perf["players"]}
    rows = []
    seen = set()
    for a in att["players"]:
        p = by_player.get(a["player_name"], {})
        seen.add(a["player_name"])
        rows.append(
            {
                "player": a["player_name"], "class": p.get("class") or a["player_class"], "spec": p.get("spec"), "role": p.get("role"),
                "raids": a["raids"], "pct": a["pct"], "kills": p.get("kills", 0), "avg": p.get("avg"), "median": p.get("median"),
                "best": p.get("best"), "avg_bracket": p.get("avg_bracket"),
            }
        )
    for p in perf["players"]:  # parsed on a kill but never in attendance (pugs, or attendance not synced yet)
        if p["player"] not in seen:
            rows.append({**p, "raids": 0, "pct": None})
    rows.sort(key=lambda r: (-(r["pct"] or 0), -(r["avg"] or 0), r["player"]))
    return {"players": rows, "total_raids": att["total_raids"], "perf": perf}


def unattributed_reports(conn: sqlite3.Connection, zone_id: int) -> int:
    """Reports in a zone that could not be assigned to any raid team (their pulls only show in the guild view)."""
    return conn.execute(
        """SELECT COUNT(*) AS c FROM reports r WHERE r.zone_id = ?
           AND NOT EXISTS (SELECT 1 FROM report_teams t WHERE t.report_code = r.code)""",
        (zone_id,),
    ).fetchone()["c"]


def team_cards(conn: sqlite3.Connection, teams: list[str]) -> list[dict[str, Any]]:
    """For the team chooser: each team's current tier and best-difficulty progress, plus the whole guild."""
    cards = []
    for team in [*teams, None]:
        cur = current_tier(conn, team)
        card: dict[str, Any] = {"team": team, "tier": cur["name"] if cur else None, "zone_id": cur["id"] if cur else None, "lines": []}
        if cur:
            for d in RAID_DIFFS:
                k = cur["kills"].get(d)
                if k is not None:
                    card["lines"].append({"difficulty": d, "name": DIFFICULTIES[d], "killed": k, "total": cur["bosses"]})
            best = best_difficulty(conn, cur["id"], team)
            s = tier_summary(conn, cur["id"], best, team)
            card["best"] = {"difficulty": best, "name": DIFFICULTIES[best], "killed": s["killed"], "total": s["total_bosses"],
                            "next_boss": s["next_boss"]["name"] if s["next_boss"] else None, "cleared": s["cleared"],
                            "last_pull_date": s["last_pull_date"]}
            card["reports"] = cur["reports"]
        cards.append(card)
    return cards


# ------------------------------------------------------------------------------------------ overview
def overview(conn: sqlite3.Connection, team: str | None = None) -> dict[str, Any]:
    """Compact snapshot used by the dashboard header and the Ask tool."""
    home = home_guild(conn)
    cur = current_tier(conn, team)
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
        "team": team,
        "teams": teams_seen(conn),
        "current_tier": cur,
        "tiers": tiers(conn, team),
        "raiderio": rio_summary,
        "last_sync": ms_to_date(int(last_sync["value"])) if last_sync else None,
        "last_sync_ms": int(last_sync["value"]) if last_sync else None,
    }
