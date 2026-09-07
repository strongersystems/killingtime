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


def rio_kills_for_zone(conn: sqlite3.Connection, zone_id: int, difficulty: int) -> dict[int, dict[str, Any]]:
    """What Raider.IO credits the *guild* with in a zone: {encounter_id: {killed, first_kill_ms}}.

    Raider.IO is the record of what the guild actually killed. Our own Warcraft Logs data only covers reports
    uploaded to the guild, so it can miss kills that were logged personally or never uploaded."""
    return {
        r["encounter_id"]: {"killed": bool(r["is_defeated"]), "first_kill_ms": r["first_defeated"], "pulls": r["num_pulls"]}
        for r in _rows(
            conn,
            """SELECT e.id AS encounter_id, p.is_defeated, p.first_defeated, p.num_pulls
               FROM encounters e
               JOIN zones z ON z.id = e.zone_id
               JOIN rio_progress p ON p.raid_slug = z.rio_raid_slug AND p.encounter_slug = e.rio_encounter_slug
               JOIN guilds g ON g.id = p.guild_id AND g.is_home = 1
               WHERE e.zone_id = ? AND p.difficulty = ? AND e.rio_encounter_slug IS NOT NULL""",
            (zone_id, difficulty),
        )
    }


def _rio_kill_counts(conn: sqlite3.Connection, zone_id: int, cutoff: int | None = None) -> dict[int, int]:
    """Bosses Raider.IO credits the guild with per difficulty in a zone, ignoring post-season kills."""
    return {
        int(r["difficulty"]): int(r["killed"] or 0)
        for r in _rows(
            conn,
            """SELECT p.difficulty, COUNT(*) AS killed
               FROM encounters e
               JOIN zones z ON z.id = e.zone_id
               JOIN rio_progress p ON p.raid_slug = z.rio_raid_slug AND p.encounter_slug = e.rio_encounter_slug
               JOIN guilds g ON g.id = p.guild_id AND g.is_home = 1
               WHERE e.zone_id = ? AND p.is_defeated = 1 AND p.difficulty IN (3,4,5)
                 AND (? IS NULL OR p.first_defeated IS NULL OR p.first_defeated <= ?)
               GROUP BY p.difficulty""",
            (zone_id, cutoff, cutoff),
        )
    }


def _before_cutoff(cutoff: int | None, column: str = "start_time") -> tuple[str, tuple]:
    """SQL fragment keeping only rows from before a tier's season cut-off."""
    return (f" AND {column} <= ?", (cutoff,)) if cutoff else ("", ())


def raid_cutoff(conn: sqlite3.Connection, raid_slug: str) -> int | None:
    """Season cut-off for a Raider.IO raid, for raids we hold no logs for. See ``zone_cutoff``."""
    row = conn.execute("SELECT cutoff_at FROM rio_raids WHERE slug = ?", (raid_slug,)).fetchone()
    cutoff = int(row["cutoff_at"]) if row and row["cutoff_at"] else None
    return cutoff if (cutoff and cutoff <= int(datetime.now(UTC).timestamp() * 1000)) else None


def zone_cutoff(conn: sqlite3.Connection, zone_id: int) -> int | None:
    """The season cut-off for a tier (ms), after which a kill earns no Cutting Edge / Ahead of the Curve.
    None while the tier is still running (a cut-off in the future, including Raider.IO's far-future placeholder for
    the live season) or when Raider.IO has no window for the raid."""
    row = conn.execute(
        """SELECT r.cutoff_at FROM zones z JOIN rio_raids r ON r.slug = z.rio_raid_slug WHERE z.id = ?""", (zone_id,)
    ).fetchone()
    cutoff = int(row["cutoff_at"]) if row and row["cutoff_at"] else None
    return cutoff if (cutoff and cutoff <= int(datetime.now(UTC).timestamp() * 1000)) else None


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
               z.rio_raid_slug AS slug
        FROM zones z LEFT JOIN expansions x ON x.id = z.expansion_id
        WHERE EXISTS (SELECT 1 FROM reports r {team_join} WHERE r.zone_id = z.id)
        ORDER BY z.id DESC
        """,
        tp,
    )
    for r in rows:
        cutoff = zone_cutoff(conn, r["id"])
        cut_sql, cut_p = _before_cutoff(cutoff, "r.start_time")
        # A tier's raid nights stop at the season cut-off too, so a post-season farm night does not extend it.
        rep = conn.execute(
            f"""SELECT COUNT(*) AS reports, MIN(r.start_time) AS first_report, MAX(r.end_time) AS last_report
                FROM reports r {team_join} WHERE r.zone_id = ?{cut_sql}""",
            (*tp, r["id"], *cut_p),
        ).fetchone()
        r["reports"], r["first_report"], r["last_report"] = rep["reports"], rep["first_report"], rep["last_report"]
        kills = _rows(
            conn,
            f"""SELECT difficulty, SUM(CASE WHEN killed = 1 AND (? IS NULL OR first_kill_time <= ?) THEN 1 ELSE 0 END) AS killed
                FROM {fk} WHERE zone_id = ? AND difficulty IN (1, 3, 4, 5){tf} GROUP BY difficulty""",
            (cutoff, cutoff, r["id"], *tp),
        )
        r["cutoff"] = cutoff
        r["cutoff_date"] = ms_to_date(cutoff)
        r["kills"] = {int(k["difficulty"]): int(k["killed"] or 0) for k in kills if k["difficulty"] is not None}
        r["logged_kills"] = dict(r["kills"])
        if not team:  # Raider.IO knows the guild, not a team: only the guild view can be topped up from it
            for d, n in _rio_kill_counts(conn, r["id"], cutoff).items():
                if n > r["kills"].get(d, 0):
                    r["kills"][d] = n
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
    """How a tier went at one difficulty.

    Two rules make the numbers match what the guild remembers:

    * **The season cut-off.** Once a tier is over, nothing after the cut-off counts - not kills, not pulls, not raid
      nights. A boss killed later is a post-season clear (see ``post_season_kills``).
    * **The true first kill.** A boss's first kill is the earliest either source knows about: our Warcraft Logs
      reports or Raider.IO. Guild logs miss nights that were logged personally, so taking the log date alone would
      count later farm wipes as progression pulls.
    """
    fk, _, tf, tp = _scope(team)
    fk_team = " AND fk.team = ?" if team else ""
    cutoff = zone_cutoff(conn, zone_id)
    cut_sql, cut_p = _before_cutoff(cutoff, "start_time")
    zone = conn.execute("SELECT * FROM zones WHERE id = ?", (zone_id,)).fetchone()
    bosses = _rows(
        conn,
        f"""SELECT e.id, e.name, e.ord, e.rio_encounter_slug, fk.killed, fk.first_kill_time, fk.first_kill_date
            FROM encounters e
            LEFT JOIN {fk} fk ON fk.encounter_id = e.id AND fk.difficulty = ?{fk_team}
            WHERE e.zone_id = ? ORDER BY e.ord""",
        (difficulty, *tp, zone_id),
    )
    rio = rio_kills_for_zone(conn, zone_id, difficulty) if not team else {}
    pulls = _rows(
        conn,
        f"""SELECT encounter_id, start_time, kill, fight_pct, duration_s, pull_date, avg_ilvl
            FROM v_pulls WHERE zone_id = ? AND difficulty = ?{tf}{cut_sql} ORDER BY start_time""",
        (zone_id, difficulty, *tp, *cut_p),
    )
    by_boss: dict[int, list[dict]] = {}
    for p in pulls:
        by_boss.setdefault(p["encounter_id"], []).append(p)

    for b in bosses:
        r = rio.get(b["id"], {})
        mine = by_boss.get(b["id"], [])
        b["rio_killed"] = bool(r.get("killed"))
        b["rio_first_kill_date"] = ms_to_date(r.get("first_kill_ms"))
        b["killed_any"] = bool(b["killed"]) or b["rio_killed"]
        # A kill Raider.IO credits the guild with that never reached our guild logs.
        b["log_missing"] = b["rio_killed"] and not b["killed"]
        b["kill_ms"] = min([t for t in (b["first_kill_time"], r.get("first_kill_ms") if b["rio_killed"] else None) if t], default=None)
        b["kill_date"] = ms_to_date(b["kill_ms"])
        # Killed after the season ended: a clear, but no Cutting Edge / Ahead of the Curve and not tier progress.
        b["post_season"] = bool(b["killed_any"] and cutoff and b["kill_ms"] and b["kill_ms"] > cutoff)
        b["counts"] = b["killed_any"] and not b["post_season"]
        b["total_pulls"] = len(mine)
        b["total_kills"] = sum(1 for p in mine if p["kill"])
        b["first_pull_time"] = mine[0]["start_time"] if mine else None
        wipe_pcts = [p["fight_pct"] for p in mine if not p["kill"] and p["fight_pct"] is not None]
        b["best_pct"] = min(wipe_pcts) if wipe_pcts else None
        # Progression = everything up to and including the first kill; later wipes on a farm boss are not progression.
        prog = [p for p in mine if b["kill_ms"] and p["start_time"] <= b["kill_ms"]] if b["counts"] else []
        prog_wipes = [p["fight_pct"] for p in prog if not p["kill"] and p["fight_pct"] is not None]
        b["pulls_to_kill"] = len(prog) or None
        # Progression that never reached our guild logs: Raider.IO still counted the pulls.
        b["rio_pulls"] = r.get("pulls") if b["counts"] and not b["pulls_to_kill"] else None
        b["wipes_before_kill"] = sum(1 for p in prog if not p["kill"]) if prog else None
        b["nights_to_kill"] = len({p["pull_date"] for p in prog}) or None
        b["hours_to_kill"] = round(sum(p["duration_s"] or 0 for p in prog) / 3600.0, 2) if prog else None
        b["best_wipe_pct"] = min(prog_wipes) if prog_wipes else None

    killed_logged = sum(1 for b in bosses if b["killed"] and not b["post_season"])
    killed = sum(1 for b in bosses if b["counts"])
    killed_all_time = sum(1 for b in bosses if b["killed_any"])
    unlogged = [b["name"] for b in bosses if b["log_missing"] and not b["post_season"]]
    post_season = [{"boss": b["name"], "date": b["kill_date"]} for b in bosses if b["post_season"]]
    final_boss = bosses[-1] if bosses else None
    earned = bool(final_boss and final_boss["counts"])
    ilvls = [p["avg_ilvl"] for p in pulls if p["avg_ilvl"]]
    first_pull = pulls[0]["start_time"] if pulls else None
    last_pull = max((p["start_time"] for p in pulls), default=None)
    # "Latest kill" means the tier's progression, so a post-season clear does not extend it.
    last_kill = max((b["kill_ms"] for b in bosses if b["counts"] and b["kill_ms"]), default=None)
    days_to_current = (last_kill - first_pull) / DAY_MS if (first_pull and last_kill) else None
    return {
        "zone": dict(zone) if zone else None,
        "team": team,
        "difficulty": difficulty,
        "difficulty_name": DIFFICULTIES.get(difficulty, str(difficulty)),
        "bosses": bosses,
        "killed": killed,
        "killed_logged": killed_logged,
        "killed_all_time": killed_all_time,
        "unlogged_kills": unlogged,
        "post_season_kills": post_season,
        "cutoff": cutoff,
        "cutoff_date": ms_to_date(cutoff),
        "tier_over": bool(cutoff),
        # Cutting Edge (Mythic) / Ahead of the Curve (Heroic) are earned by killing the final boss before the cut-off.
        "achievement": ("Cutting Edge" if difficulty == 5 else "Ahead of the Curve" if difficulty == 4 else None),
        "achievement_earned": earned if difficulty in (4, 5) else None,
        "total_bosses": len(bosses),
        "cleared": killed == len(bosses) and killed > 0,
        "pulls": len(pulls),
        "kills": sum(1 for p in pulls if p["kill"]),
        "wipes": sum(1 for p in pulls if not p["kill"]),
        "nights": len({p["pull_date"] for p in pulls}),
        "hours": round(sum(p["duration_s"] or 0 for p in pulls) / 3600.0, 1),
        "avg_ilvl": round(sum(ilvls) / len(ilvls), 1) if ilvls else None,
        "first_pull_date": ms_to_date(first_pull),
        "last_pull_date": ms_to_date(last_pull),
        "last_kill_date": ms_to_date(last_kill),
        "days_to_latest_kill": round(days_to_current, 1) if days_to_current is not None else None,
        "next_boss": next((b for b in bosses if not b["counts"]), None),
    }


def progress_timeline(conn: sqlite3.Connection, zone_id: int, difficulty: int, team: str | None = None) -> list[dict[str, Any]]:
    """Cumulative bosses killed by day, plus day index since our first pull in this zone/difficulty."""
    fk, _, tf, tp = _scope(team)
    cutoff = zone_cutoff(conn, zone_id)
    cut_sql, cut_p = _before_cutoff(cutoff, "start_time")
    first = conn.execute(
        f"SELECT MIN(start_time) AS t FROM v_pulls WHERE zone_id = ? AND difficulty = ?{tf}{cut_sql}",
        (zone_id, difficulty, *tp, *cut_p),
    ).fetchone()["t"]
    kill_cut, kill_p = _before_cutoff(cutoff, "first_kill_time")
    kills = _rows(
        conn,
        f"""SELECT encounter_name, first_kill_time FROM {fk}
            WHERE zone_id = ? AND difficulty = ? AND killed = 1{tf}{kill_cut} ORDER BY first_kill_time""",
        (zone_id, difficulty, *tp, *kill_p),
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
                    "killed": bool(b["counts"]),
                    "pulls_to_kill": b["pulls_to_kill"],
                    "cum_pulls": cum_pulls,
                    "days_to_kill": round((b["kill_ms"] - first_pull) / DAY_MS, 1)
                    if (b["counts"] and b["kill_ms"] and first_pull)
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
    cut_sql, cut_p = _before_cutoff(zone_cutoff(conn, zone_id), "n.first_pull_time")
    return _rows(
        conn,
        f"""SELECT pull_date, difficulty, pulls, kills, wipes, bosses_pulled, ROUND(hours_in_combat, 2) AS hours_in_combat,
                   ROUND(avg_ilvl, 1) AS avg_ilvl,
                   (SELECT GROUP_CONCAT(DISTINCT encounter_name) FROM v_pulls p
                     WHERE p.zone_id = n.zone_id AND p.difficulty = n.difficulty AND p.pull_date = n.pull_date AND p.kill = 1{p_team}) AS killed_bosses
            FROM {rn} n WHERE zone_id = ?{n_team}{cut_sql} ORDER BY pull_date DESC, difficulty DESC LIMIT ?""",
        (zone_id, *tp, *cut_p, limit),
    )


def attendance_summary(conn: sqlite3.Connection, zone_id: int, team: str | None = None) -> dict[str, Any]:
    _, _, tf, tp = _scope(team)
    cut_sql, cut_p = _before_cutoff(zone_cutoff(conn, zone_id), "start_time")
    # Raids are counted by date: two people logging the same night produce two reports of one raid.
    total_raids = conn.execute(
        f"SELECT COUNT(DISTINCT raid_date) AS c FROM v_attendance WHERE zone_id = ?{tf}{cut_sql}", (zone_id, *tp, *cut_p)
    ).fetchone()["c"]
    players = _rows(
        conn,
        f"""SELECT player_name, player_class, COUNT(DISTINCT raid_date) AS raids,
                   ROUND(100.0 * COUNT(DISTINCT raid_date) / ?, 1) AS pct
            FROM v_attendance WHERE zone_id = ? AND presence = 1{tf}{cut_sql}
            GROUP BY player_name ORDER BY raids DESC, player_name""",
        (max(total_raids, 1), zone_id, *tp, *cut_p),
    )
    return {"total_raids": total_raids, "players": players}


def latest_kills(conn: sqlite3.Connection, limit: int = 10, team: str | None = None, zone_id: int | None = None) -> list[dict[str, Any]]:
    """Most recent first kills (Normal/Heroic/Mythic), newest first, with the team that got each kill."""
    fk, _, tf, tp = _scope(team)
    zf = " AND fk.zone_id = ?" if zone_id else ""
    zp = (zone_id,) if zone_id else ()
    cut_sql, cut_p = _before_cutoff(zone_cutoff(conn, zone_id) if zone_id else None, "fk.first_kill_time")
    team_col = "fk.team" if team else (
        "(SELECT p.team FROM v_pulls p WHERE p.encounter_id = fk.encounter_id AND p.difficulty = fk.difficulty "
        "AND p.kill = 1 AND p.start_time = fk.first_kill_time LIMIT 1)"
    )
    rows = _rows(
        conn,
        f"""SELECT fk.zone_id, fk.zone_name, fk.encounter_name AS boss, fk.encounter_ord, fk.difficulty, fk.first_kill_time,
                   fk.first_kill_date AS date, fk.pulls_to_kill AS pulls, fk.nights_to_kill AS nights, {team_col} AS team
            FROM {fk} fk WHERE fk.killed = 1 AND fk.difficulty IN (3,4,5){tf.replace('team', 'fk.team')}{zf}{cut_sql}
            ORDER BY fk.first_kill_time DESC LIMIT ?""",
        (*tp, *zp, *cut_p, limit),
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
    """Our per-boss progress keyed by Raider.IO boss slug.

    Taken from ``tier_summary`` when the raid is mapped to one of our zones, so the Peers page uses exactly the same
    kills, pulls and season cut-off as the Progress page. Falls back to Raider.IO for raids we have no logs for.
    """
    zone = conn.execute("SELECT id FROM zones WHERE rio_raid_slug = ?", (raid_slug,)).fetchone()
    out: dict[str, dict[str, Any]] = {}
    if zone:
        for b in tier_summary(conn, zone["id"], difficulty, team)["bosses"]:
            if not b["rio_encounter_slug"]:
                continue
            out[b["rio_encounter_slug"]] = {
                "killed": bool(b["counts"]),
                "pulls": (b["pulls_to_kill"] if b["counts"] else b["total_pulls"]) or None,
                "first_kill_ms": b["kill_ms"] if b["counts"] else None,
                "started_ms": b["first_pull_time"],
                "source": "logs",
            }
    if out and any(v["pulls"] for v in out.values()):
        return out
    if team:  # a team without logs in this raid has no progress of its own
        return out
    home = home_guild(conn)
    if not home:
        return out
    cutoff = raid_cutoff(conn, raid_slug)
    for p in _rows(conn, "SELECT * FROM rio_progress WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?", (home["id"], raid_slug, difficulty)):
        # The season cut-off applies here too: a post-season clear is not tier progress.
        killed = bool(p["is_defeated"]) and not (cutoff and p["first_defeated"] and p["first_defeated"] > cutoff)
        out[p["encounter_slug"]] = {
            "killed": killed,
            "pulls": p["num_pulls"] or None,
            "first_kill_ms": p["first_defeated"] if killed else None,
            "started_ms": p["pull_started_at"] or p["first_defeated"],
            "source": "raider.io",
        }
    return out


PEER_MODES = {
    "level": "Similar progress",
    "rank": "Around our realm rank",
    "cohort": "Last tier's neighbours",
}


def _realm_position(candidates: list[dict], kills: int, latest_ms: int | None) -> int | None:
    """Estimate a realm rank from a kill count: Raider.IO ranks by bosses killed, then by who killed them first."""
    if kills <= 0:
        return None
    ahead = sum(
        1 for c in candidates
        if c["killed"] > kills or (c["killed"] == kills and latest_ms and c["latest_ms"] and c["latest_ms"] < latest_ms)
    )
    return ahead + 1


def _realm_candidates(conn: sqlite3.Connection, raid_slug: str, difficulty: int, home_id: int) -> list[dict]:
    """Other guilds with Raider.IO progress in a raid/difficulty, with their kill count, ranks and per-boss rows."""
    guild_rows = _rows(
        conn,
        """SELECT g.id, g.name, g.realm_slug, g.region, g.is_rival, rk.realm_rank, rk.world_rank
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
    out = []
    for g in guild_rows:
        prog = by_guild.get(g["id"], {})
        starts = [p["pull_started_at"] or p["first_defeated"] for p in prog.values() if (p["pull_started_at"] or p["first_defeated"])]
        kills = [p["first_defeated"] for p in prog.values() if p["is_defeated"] and p["first_defeated"]]
        out.append({**g, "killed": sum(1 for p in prog.values() if p["is_defeated"]), "prog": prog,
                    "start_ms": min(starts) if starts else None, "latest_ms": max(kills) if kills else None,
                    "has_pulls": any((p["num_pulls"] or 0) > 0 for p in prog.values())})
    return out


def our_realm_rank(conn: sqlite3.Connection, raid_slug: str, difficulty: int, team: str | None, ours: dict | None = None,
                   candidates: list[dict] | None = None) -> tuple[int | None, bool, int]:
    """(realm rank, estimated?, kills). Raider.IO ranks the guild; when a team's progress differs from the guild's,
    the team's rank is estimated from its own kill count against the realm leaderboard."""
    home = home_guild(conn)
    home_id = home["id"] if home else -1
    ours = ours if ours is not None else _our_boss_progress(conn, raid_slug, difficulty, team)
    our_kills = sum(1 for v in ours.values() if v.get("killed"))
    guild_kills = conn.execute(
        "SELECT COUNT(*) AS c FROM rio_progress WHERE guild_id = ? AND raid_slug = ? AND difficulty = ? AND is_defeated = 1",
        (home_id, raid_slug, difficulty),
    ).fetchone()["c"]
    rank_row = conn.execute(
        "SELECT realm_rank FROM rio_rankings WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?", (home_id, raid_slug, difficulty)
    ).fetchone()
    if rank_row and rank_row["realm_rank"] and (not team or our_kills == guild_kills):
        return int(rank_row["realm_rank"]), False, our_kills
    cands = candidates if candidates is not None else _realm_candidates(conn, raid_slug, difficulty, home_id)
    realm = [c for c in cands if home and c["realm_slug"] == home["realm_slug"] and c["region"] == home["region"]]
    latest = max((v["first_kill_ms"] for v in ours.values() if v.get("first_kill_ms")), default=None)
    return _realm_position(realm, our_kills, latest), True, our_kills


def peer_comparison(
    conn: sqlite3.Connection, raid_slug: str, difficulty: int, team: str | None = None, min_peers: int = 8, max_peers: int = 40,
    mode: str = "level", above: int = 20, below: int = 20, prev_raid_slug: str | None = None,
) -> dict[str, Any]:
    """Compare us with a peer group of other guilds (realm leaderboard + rivals), chosen by ``mode``:

    * ``level``: guilds whose kill count is within a band of ours (the band widens until ``min_peers`` qualify),
      nearest realm ranks first - "guilds around our level", not the realm's top.
    * ``rank``: guilds on our realm ranked from ``above`` places above us to ``below`` places below us.
    * ``cohort``: the guilds that were within ``above``/``below`` realm ranks of us in ``prev_raid_slug`` (last tier),
      wherever they are now - did we move with, past or behind last tier's neighbours?

    Per boss: our pulls vs the peers' average/median/quartiles, the share of peers we out-pulled (percentile), and
    days from the guild's first pull in the raid to the kill vs the peers' typical."""
    bosses = _rows(conn, "SELECT slug, name, ord FROM rio_encounters WHERE raid_slug = ? ORDER BY ord", (raid_slug,))
    # Raider.IO occasionally lists fewer bosses than Warcraft Logs, so say which ones the comparison leaves out.
    untracked = [
        r["name"] for r in _rows(
            conn,
            """SELECT e.name FROM encounters e JOIN zones z ON z.id = e.zone_id
               WHERE z.rio_raid_slug = ? AND e.rio_encounter_slug IS NULL ORDER BY e.ord""", (raid_slug,))
    ]
    home = home_guild(conn)
    home_id = home["id"] if home else -1
    ours = _our_boss_progress(conn, raid_slug, difficulty, team)
    our_kills = sum(1 for b in bosses if ours.get(b["slug"], {}).get("killed"))
    our_start = min((v["started_ms"] for v in ours.values() if v.get("started_ms")), default=None)

    candidates = _realm_candidates(conn, raid_slug, difficulty, home_id)
    home_realm = [c for c in candidates if home and c["realm_slug"] == home["realm_slug"] and c["region"] == home["region"]]
    our_rank, rank_estimated, _ = our_realm_rank(conn, raid_slug, difficulty, team, ours, candidates)
    above, below = max(0, int(above)), max(0, int(below))

    band = 0
    peers: list[dict] = []
    prev: dict[str, Any] | None = None
    if mode == "rank":
        if our_rank:
            peers = sorted((c for c in home_realm if c["realm_rank"] and our_rank - above <= c["realm_rank"] <= our_rank + below),
                           key=lambda c: c["realm_rank"])
    elif mode == "cohort":
        prev = {"raid_slug": prev_raid_slug, "our_rank": None, "estimated": False, "missing": 0, "raid_name": None}
        if prev_raid_slug:
            prev_row = conn.execute("SELECT name FROM rio_raids WHERE slug = ?", (prev_raid_slug,)).fetchone()
            prev["raid_name"] = prev_row["name"] if prev_row else prev_raid_slug
            prev_cands = _realm_candidates(conn, prev_raid_slug, difficulty, home_id)
            prev_rank, prev_est, _ = our_realm_rank(conn, prev_raid_slug, difficulty, team, candidates=prev_cands)
            prev.update(our_rank=prev_rank, estimated=prev_est)
            if prev_rank:
                cohort = {c["id"]: c["realm_rank"] for c in prev_cands
                          if c["realm_rank"] and home and c["realm_slug"] == home["realm_slug"] and c["region"] == home["region"]
                          and prev_rank - above <= c["realm_rank"] <= prev_rank + below}
                now = {c["id"]: c for c in candidates}
                for gid, r in cohort.items():
                    if gid in now:
                        now[gid]["prev_rank"] = r
                        peers.append(now[gid])
                prev["missing"] = len(cohort) - len(peers)
                peers.sort(key=lambda c: c["prev_rank"])
    else:
        mode = "level"
        while band <= len(bosses):
            peers = [c for c in candidates if abs(c["killed"] - our_kills) <= band and c["killed"] > 0]
            if len(peers) >= min_peers:
                break
            band += 1
        # Closest kill counts first, then the guilds ranked nearest to us (or to the middle of the band).
        ranks = sorted(c["realm_rank"] for c in peers if c["realm_rank"])
        reference = our_rank or (ranks[len(ranks) // 2] if ranks else 0)
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
        "untracked_bosses": untracked,
        "band": band,
        "mode": mode,
        "mode_label": PEER_MODES.get(mode, mode),
        "above": above,
        "below": below,
        "our_rank": our_rank,
        "rank_estimated": rank_estimated,
        "prev": prev,
        "peers_ahead": sum(1 for c in peers if c["realm_rank"] and our_rank and c["realm_rank"] < our_rank),
        "peers_behind": sum(1 for c in peers if c["realm_rank"] and our_rank and c["realm_rank"] > our_rank),
        "peer_count": len(peers),
        "peer_kills_median": _median([c["killed"] for c in peers]),
        "our_total_pulls": our_total or None,
        "peer_median_total_pulls": _median(peer_totals),
        "overall_beat_pct": overall_beat,
        "bosses": per_boss,
        "peers": [
            {"name": c["name"], "realm": c["realm_slug"], "killed": c["killed"], "realm_rank": c["realm_rank"], "world_rank": c["world_rank"],
             "is_rival": bool(c["is_rival"]), "pulls": sum((p["num_pulls"] or 0) for p in c["prog"].values()) or None,
             "latest_kill": ms_to_date(c["latest_ms"]), "prev_rank": c.get("prev_rank"),
             "rank_delta": (c["prev_rank"] - c["realm_rank"]) if (c.get("prev_rank") and c["realm_rank"]) else None}
            for c in peers
        ],
    }


# ------------------------------------------------------------------------------------------ parses
def performance(
    conn: sqlite3.Connection, zone_id: int, difficulty: int | None = None, team: str | None = None, include_pugs: bool = False
) -> dict[str, Any]:
    """Parse summary for a tier: per player (avg/median/best rank percentile), per boss and per raid night."""
    where = ["zone_id = ?", "difficulty IN (3,4,5)"]
    params: list[Any] = [zone_id]
    if difficulty:
        where.append("difficulty = ?")
        params.append(difficulty)
    if team:
        where.append("team = ?")
        params.append(team)
    if not include_pugs:
        # A guild member can play on another realm, so membership is decided by attendance in this
        # tier, not by the realm printed on the parse.
        where.append("player_name IN (SELECT player_name FROM v_attendance WHERE zone_id = ?)")
        params.append(zone_id)
    cutoff = zone_cutoff(conn, zone_id)
    if cutoff:
        where.append("start_time <= ?")
        params.append(cutoff)
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


def _slug(name: str) -> str:
    import re as _re
    import unicodedata as _ud

    plain = "".join(c for c in _ud.normalize("NFKD", name) if not _ud.combining(c))
    return _re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def career_history(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every raider's history from our own logs: tiers raided, nights per tier and how they parsed in each."""
    tiers_by_id = {t["id"]: t for t in _rows(conn, "SELECT id, name FROM zones")}
    per_tier: dict[str, dict[int, dict]] = {}
    for r in _rows(
        conn,
        """SELECT player_name, zone_id, COUNT(DISTINCT raid_date) AS raids,
                  MIN(start_time) AS first_ms, MAX(start_time) AS last_ms
           FROM v_attendance WHERE presence = 1 GROUP BY player_name, zone_id""",
    ):
        per_tier.setdefault(r["player_name"], {})[r["zone_id"]] = {
            "zone_id": r["zone_id"], "tier": (tiers_by_id.get(r["zone_id"]) or {}).get("name"),
            "raids": r["raids"], "first_ms": r["first_ms"], "last_ms": r["last_ms"], "parse": None, "kills": 0,
        }
    for r in _rows(
        conn,
        """SELECT player_name, zone_id, AVG(rank_percent) AS parse, COUNT(*) AS kills
           FROM v_parses WHERE rank_percent IS NOT NULL GROUP BY player_name, zone_id""",
    ):
        row = per_tier.setdefault(r["player_name"], {}).setdefault(r["zone_id"], {
            "zone_id": r["zone_id"], "tier": (tiers_by_id.get(r["zone_id"]) or {}).get("name"),
            "raids": 0, "first_ms": None, "last_ms": None, "parse": None, "kills": 0})
        row["parse"] = round(r["parse"], 1)
        row["kills"] = r["kills"]
    out = {}
    for player, tiers in per_tier.items():
        rows = sorted(tiers.values(), key=lambda t: t["zone_id"], reverse=True)
        firsts = [t["first_ms"] for t in rows if t["first_ms"]]
        out[player] = {
            "tiers": [{**t, "first": ms_to_date(t["first_ms"]), "last": ms_to_date(t["last_ms"])} for t in rows],
            "tier_count": sum(1 for t in rows if t["raids"]),
            "total_raids": sum(t["raids"] for t in rows),
            "since": ms_to_date(min(firsts)) if firsts else None,
            "first_tier": next((t["tier"] for t in reversed(rows) if t["raids"]), None),
        }
    return out


# Warcraft Logs reports a player's role from what they did in the fight, which mislabels healers who were told to
# help with damage. The spec is unambiguous, so it wins when we know it.
HEALER_SPECS = {"Restoration", "Holy", "Discipline", "Mistweaver", "Preservation"}
TANK_SPECS = {"Protection", "Blood", "Guardian", "Vengeance", "Brewmaster"}


def role_from_spec(spec: str | None, cls: str | None, fallback: str | None) -> str | None:
    """tanks / healers / dps from the spec name (Holy is a healer on a Priest or Paladin, never a Warrior)."""
    if not spec:
        return fallback
    if spec in TANK_SPECS:
        return "tanks"
    if spec in HEALER_SPECS:
        return "healers" if spec != "Holy" or cls in {"Priest", "Paladin"} else "dps"
    return "dps"


def meet_the_team(conn: sqlite3.Connection, zone_id: int, difficulty: int | None = None, team: str | None = None,
                   min_raids: int = 2, alts: dict[str, list[str]] | None = None,
                   image_url: str = "/static/members/{slug}/{n}.webp") -> list[dict[str, Any]]:
    """One card per raider for the Meet the Team page: their numbers, their character, a bio and a portrait prompt.

    Only people who actually raid this tier (``min_raids`` nights, or any parse), sorted by attendance."""
    import json as _json

    from . import flavour

    alts = alts or {}
    home = home_guild(conn)
    guild_name = home["name"] if home else ""
    realm = f"{(home['realm_slug'] if home else '').title()} {(home['region'] if home else '').upper()}".strip()
    zone = conn.execute("SELECT name, rio_raid_slug FROM zones WHERE id = ?", (zone_id,)).fetchone()
    zone_slug = zone["rio_raid_slug"] if zone else None
    data = roster(conn, zone_id, difficulty, team)
    # Characters are keyed by name: a guild member may be on another realm, and names are unique within a raid.
    chars = {r["name"]: r for r in _rows(conn, "SELECT * FROM characters WHERE missing = 0")}
    history = career_history(conn)
    cut_sql, cut_p = _before_cutoff(zone_cutoff(conn, zone_id), "start_time")
    df_sql, df_p = (" AND difficulty = ?", (difficulty,)) if difficulty else ("", ())
    tm_sql, tm_p = (" AND team = ?", (team,)) if team else ("", ())
    per_boss: dict[str, list[dict]] = {}
    for r in _rows(
        conn,
        f"""SELECT player_name, encounter_name, AVG(rank_percent) AS pct, COUNT(*) AS kills
            FROM v_parses WHERE zone_id = ? AND rank_percent IS NOT NULL{df_sql}{tm_sql}{cut_sql}
            GROUP BY player_name, encounter_name""",
        (zone_id, *df_p, *tm_p, *cut_p),
    ):
        per_boss.setdefault(r["player_name"], []).append(r)

    cards = []
    for p in data["players"]:
        if (p["raids"] or 0) < min_raids and not p["kills"]:
            continue
        bosses = sorted(per_boss.get(p["player"], []), key=lambda b: b["pct"]) if p["kills"] else []
        c = chars.get(p["player"], {})
        card = {
            **p,
            "team": team,
            "race": c.get("race"), "gender": c.get("gender"),
            "class": c.get("class") or p.get("class"),
            "spec": c.get("spec") or p.get("spec"),
            "item_level": c.get("item_level"),
            "portrait_url": c.get("portrait_url"), "thumbnail_url": c.get("thumbnail_url"),
            "profile_url": c.get("profile_url"),
            "gear": _json.loads(c["gear"]) if c.get("gear") else {},
            "role": role_from_spec(c.get("spec") or p.get("spec"), c.get("class") or p.get("class"), p.get("role")),
            "best_boss": {"boss": bosses[-1]["encounter_name"], "pct": bosses[-1]["pct"]} if bosses else None,
            "worst_boss": {"boss": bosses[0]["encounter_name"], "pct": bosses[0]["pct"]} if len(bosses) > 1 else None,
        }
        card["weapons"] = [card["gear"][s]["name"] for s in ("mainhand", "offhand")
                           if card["gear"].get(s) and card["gear"][s].get("name")]
        card["history"] = history.get(p["player"], {"tiers": [], "tier_count": 0, "total_raids": 0})
        card["mplus_score"] = c.get("mplus_score")
        card["mplus_role"] = c.get("mplus_role")
        card["mplus_best"] = _json.loads(c["mplus_best"]) if c.get("mplus_best") else []
        card["achievement_points"] = c.get("achievement_points")
        prog = _json.loads(c["raid_progression"]) if c.get("raid_progression") else {}
        card["personal_progress"] = prog.get(zone_slug) if zone_slug else None
        card["alts"] = alts.get(p["player"], [])
        card["stats"] = flavour.stats(card)
        card["bio"] = flavour.bio(card)
        prompts = flavour.portrait_prompts(card, guild_name, realm, zone["name"] if zone else None)
        card["portrait_prompts"] = prompts
        card["portrait_prompt"] = prompts[0]["prompt"]
        slug = _slug(p["player"])
        card["slug"] = slug
        card["frames"] = [image_url.format(slug=slug, n=i + 1) for i in range(len(prompts))] if image_url else []
        cards.append(card)
    return cards


def boss_pulls(conn: sqlite3.Connection, zone_id: int, encounter_id: int, difficulty: int,
               team: str | None = None) -> dict[str, Any]:
    """Every pull on one boss, in order, with the running best percentage.

    This is the progression view for the boss we are actually working on: pull 1 to pull n, what each wipe got to,
    which night it was, and where the best pull moved. ``pct_left`` is how much of the fight was left, so lower is
    better and a kill is 0."""
    _, _, tf, tp = _scope(team)
    cut_sql, cut_p = _before_cutoff(zone_cutoff(conn, zone_id), "start_time")
    boss = conn.execute(
        "SELECT e.id, e.name, e.ord, e.zone_id, z.name AS zone_name FROM encounters e "
        "JOIN zones z ON z.id = e.zone_id WHERE e.id = ?", (encounter_id,)
    ).fetchone()
    rows = _rows(
        conn,
        f"""SELECT report_code, fight_id, start_time, end_time, kill, fight_pct, boss_pct, last_phase,
                   duration_s, pull_date, avg_ilvl, size
            FROM v_pulls WHERE zone_id = ? AND encounter_id = ? AND difficulty = ?{tf}{cut_sql}
            ORDER BY start_time""",
        (zone_id, encounter_id, difficulty, *tp, *cut_p),
    )
    pulls, best_so_far, nights = [], None, []
    for i, r in enumerate(rows, start=1):
        pct = 0.0 if r["kill"] else r["fight_pct"]
        if pct is not None and (best_so_far is None or pct < best_so_far):
            best_so_far = pct
        if not nights or nights[-1]["date"] != r["pull_date"]:
            nights.append({"date": r["pull_date"], "first_pull": i, "pulls": 0, "best": None, "kill": False})
        night = nights[-1]
        night["pulls"] += 1
        night["kill"] = night["kill"] or bool(r["kill"])
        if pct is not None and (night["best"] is None or pct < night["best"]):
            night["best"] = pct
        pulls.append({
            "n": i, "date": r["pull_date"], "start_time": r["start_time"], "kill": bool(r["kill"]),
            "pct_left": round(pct, 2) if pct is not None else None,
            "boss_pct": round(r["boss_pct"], 2) if r["boss_pct"] is not None else None,
            "phase": r["last_phase"], "duration_s": round(r["duration_s"] or 0),
            "best_so_far": round(best_so_far, 2) if best_so_far is not None else None,
            "report_code": r["report_code"], "fight_id": r["fight_id"],
            "log_url": f"https://www.warcraftlogs.com/reports/{r['report_code']}#fight={r['fight_id']}",
        })
    wipes = [p for p in pulls if not p["kill"]]
    kill = next((p for p in pulls if p["kill"]), None)
    best = min((p["pct_left"] for p in wipes if p["pct_left"] is not None), default=None)
    ilvls = [r["avg_ilvl"] for r in rows if r["avg_ilvl"]]
    return {
        "boss": dict(boss) if boss else None,
        "difficulty": difficulty,
        "difficulty_name": DIFFICULTIES.get(difficulty, str(difficulty)),
        "team": team,
        "pulls": pulls,
        "nights": nights,
        "total_pulls": len(pulls),
        "wipes": len(wipes),
        "killed": bool(kill),
        "kill_pull": kill["n"] if kill else None,
        "kill_date": kill["date"] if kill else None,
        "best_pct": best,
        "best_pull": next((p["n"] for p in wipes if p["pct_left"] == best), None) if best is not None else None,
        "last_pct": pulls[-1]["pct_left"] if pulls else None,
        "hours": round(sum(p["duration_s"] for p in pulls) / 3600.0, 1),
        "longest_s": max((p["duration_s"] for p in pulls), default=0),
        "avg_ilvl": round(sum(ilvls) / len(ilvls), 1) if ilvls else None,
        "first_pull_date": pulls[0]["date"] if pulls else None,
        "last_pull_date": pulls[-1]["date"] if pulls else None,
    }


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
