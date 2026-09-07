"""Pull data from Warcraft Logs and Raider.IO into SQLite.

Run with ``kt sync`` (incremental) or ``kt sync --full``. Every step is independent and
tolerant of the other source being unavailable, so the app works with only Warcraft Logs
credentials, only Raider.IO (no credentials needed), or both.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import GuildRef, Settings
from .db import RIO_DIFFICULTY_TO_CODE, set_meta, transaction
from .raiderio import RaiderIOClient, RaiderIOError
from .wcl import WCLClient, WCLError

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class SyncStats:
    zones: int = 0
    encounters: int = 0
    reports: int = 0
    fights: int = 0
    attendance_rows: int = 0
    parses: int = 0
    characters: int = 0
    rio_guilds: int = 0
    rio_progress_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    wcl_queries: int = 0
    rio_requests: int = 0

    def warn(self, msg: str, progress: Progress | None = None) -> None:
        self.warnings.append(msg)
        log.warning(msg)
        if progress:
            progress("warning: " + msg)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", ""))
    return s.strip("-")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("the ", ""))


def match_name(target: str, candidates: dict[str, str], cutoff: float = 0.8) -> str | None:
    """Match ``target`` (a name) to one of ``candidates`` {slug: name}. Exact slug/norm first, then fuzzy."""
    t_slug, t_norm = slugify(target), _norm(target)
    for slug, name in candidates.items():
        if slug == t_slug or _norm(name) == t_norm or slug.replace("-", "") == t_norm:
            return slug
    # One name contained in the other, e.g. WCL "VS / DR / MQD" vs Raider.IO "MN Tier 1 (VS / DR / MQD)".
    if len(t_norm) >= 4:
        contained = [slug for slug, name in candidates.items() if t_norm in _norm(name) or _norm(name) in t_norm]
        if len(contained) == 1:
            return contained[0]
    best, best_ratio = None, 0.0
    for slug, name in candidates.items():
        ratio = max(
            difflib.SequenceMatcher(None, t_norm, _norm(name)).ratio(),
            difflib.SequenceMatcher(None, t_norm, slug.replace("-", "")).ratio(),
        )
        if ratio > best_ratio:
            best, best_ratio = slug, ratio
    return best if best_ratio >= cutoff else None


# ---------------------------------------------------------------------------------------- guilds
def upsert_guild(
    conn: sqlite3.Connection,
    ref: GuildRef,
    *,
    faction: str | None = None,
    wcl_id: int | None = None,
    rio_id: int | None = None,
    is_home: bool | None = None,
    is_rival: bool | None = None,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO guilds(name, realm_slug, region) VALUES (?, ?, ?)",
        (ref.name, ref.realm_slug.lower(), ref.region.lower()),
    )
    row = conn.execute(
        "SELECT id FROM guilds WHERE lower(name) = lower(?) AND realm_slug = ? AND region = ?",
        (ref.name, ref.realm_slug.lower(), ref.region.lower()),
    ).fetchone()
    gid = int(row["id"])
    sets, params = [], []
    if faction is not None:
        sets.append("faction = ?")
        params.append(faction.lower())
    if wcl_id is not None:
        sets.append("wcl_id = ?")
        params.append(wcl_id)
    if rio_id is not None:
        sets.append("rio_id = ?")
        params.append(rio_id)
    if is_home is not None:
        sets.append("is_home = ?")
        params.append(int(is_home))
    if is_rival is not None:
        sets.append("is_rival = ?")
        params.append(int(is_rival))
    if sets:
        conn.execute(f"UPDATE guilds SET {', '.join(sets)} WHERE id = ?", (*params, gid))
    return gid


# ------------------------------------------------------------------------------------------ WCL
RAID_DIFFICULTIES = {3, 4, 5}


def is_raid_zone(zone: dict) -> bool:
    """Warcraft Logs lists dungeon seasons (difficulty 10) and Delves as zones too; raids offer Normal/Heroic/Mythic.
    Zones without difficulty data are kept (older payloads and test fixtures)."""
    ids = {int(d["id"]) for d in (zone.get("difficulties") or []) if d.get("id") is not None}
    return not ids or bool(ids & RAID_DIFFICULTIES)


def drop_zone(conn: sqlite3.Connection, zone_id: int) -> None:
    """Remove a zone and everything synced under it (used when a stored zone turns out not to be a raid)."""
    conn.execute("DELETE FROM attendance WHERE report_code IN (SELECT code FROM reports WHERE zone_id = ?)", (zone_id,))
    conn.execute("DELETE FROM fights WHERE report_code IN (SELECT code FROM reports WHERE zone_id = ?)", (zone_id,))
    conn.execute("DELETE FROM reports WHERE zone_id = ?", (zone_id,))
    conn.execute("DELETE FROM wcl_zone_rankings WHERE zone_id = ?", (zone_id,))
    conn.execute("DELETE FROM encounters WHERE zone_id = ?", (zone_id,))
    conn.execute("DELETE FROM zones WHERE id = ?", (zone_id,))


def sync_zones(conn: sqlite3.Connection, wcl: WCLClient, n_expansions: int, stats: SyncStats, progress: Progress) -> None:
    expansions = sorted(wcl.expansions(), key=lambda e: e["id"], reverse=True)[: max(1, n_expansions)]
    with transaction(conn):
        for exp in expansions:
            conn.execute(
                "INSERT INTO expansions(id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                (exp["id"], exp["name"]),
            )
            zones = wcl.zones(exp["id"])
            raids = [z for z in zones if is_raid_zone(z)]
            progress(f"expansion {exp['name']}: {len(raids)} raid zones")
            for z in zones:
                if z in raids:
                    continue
                if conn.execute("SELECT 1 FROM zones WHERE id = ?", (z["id"],)).fetchone():
                    drop_zone(conn, int(z["id"]))
                    progress(f"dropped non-raid zone {z['name']} (#{z['id']}) and its reports")
            for z in raids:
                conn.execute(
                    """INSERT INTO zones(id, name, expansion_id, frozen) VALUES (?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET name = excluded.name, expansion_id = excluded.expansion_id,
                       frozen = excluded.frozen""",
                    (z["id"], z["name"], exp["id"], int(bool(z.get("frozen")))),
                )
                stats.zones += 1
                for i, enc in enumerate(z.get("encounters") or []):
                    conn.execute(
                        """INSERT INTO encounters(id, zone_id, name, ord) VALUES (?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET zone_id = excluded.zone_id, name = excluded.name, ord = excluded.ord""",
                        (enc["id"], z["id"], enc["name"], i + 1),
                    )
                    stats.encounters += 1


def sync_home_guild_wcl(conn: sqlite3.Connection, wcl: WCLClient, settings: Settings, stats: SyncStats, progress: Progress) -> int:
    ref = settings.home_guild
    info = wcl.guild(ref.name, ref.realm_slug, ref.region.upper())
    if not info:
        raise WCLError(
            f"Guild '{ref.name}' on {ref.realm_slug}/{ref.region.upper()} not found on Warcraft Logs. "
            "Check GUILD_NAME / GUILD_REALM / GUILD_REGION."
        )
    faction = (info.get("faction") or {}).get("name")
    with transaction(conn):
        gid = upsert_guild(conn, ref, faction=faction, wcl_id=int(info["id"]), is_home=True)
    progress(f"home guild: {info['name']} (WCL id {info['id']}, {faction})")
    return gid


def sync_reports(
    conn: sqlite3.Connection, wcl: WCLClient, guild_id: int, wcl_guild_id: int, full: bool, stats: SyncStats, progress: Progress
) -> list[int]:
    """Fetch report list (incrementally) and boss fights for new/updated reports. Returns touched zone ids."""
    since: float | None = None
    if not full:
        row = conn.execute("SELECT MAX(end_time) AS m FROM reports WHERE guild_id = ?", (guild_id,)).fetchone()
        if row and row["m"]:
            since = float(row["m"]) - 3 * 86400_000  # re-check the last 3 days for late uploads/edits
    known_zones = {r["id"] for r in conn.execute("SELECT id FROM zones")}
    reports = wcl.all_reports(wcl_guild_id, start_time=since)
    progress(f"reports listed: {len(reports)}" + (f" since {datetime.fromtimestamp(since / 1000, UTC):%Y-%m-%d}" if since else ""))
    touched: set[int] = set()
    with transaction(conn):
        for rep in reports:
            zone = rep.get("zone") or {}
            zid = zone.get("id")
            if not zid or zid not in known_zones:
                continue  # dungeons, PvP, or zones from expansions we don't track
            conn.execute(
                """INSERT INTO reports(code, guild_id, zone_id, title, owner, start_time, end_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET zone_id = excluded.zone_id, title = excluded.title,
                       end_time = excluded.end_time,
                       fights_synced_at = CASE WHEN excluded.end_time > reports.end_time THEN NULL ELSE reports.fights_synced_at END,
                       rankings_synced_at = CASE WHEN excluded.end_time > reports.end_time THEN NULL ELSE reports.rankings_synced_at END""",
                (
                    rep["code"],
                    guild_id,
                    zid,
                    rep.get("title"),
                    (rep.get("owner") or {}).get("name"),
                    int(rep["startTime"]),
                    int(rep["endTime"]),
                ),
            )
            stats.reports += 1
            touched.add(int(zid))

    pending = [
        r["code"]
        for r in conn.execute(
            "SELECT code FROM reports WHERE guild_id = ? AND fights_synced_at IS NULL ORDER BY start_time", (guild_id,)
        )
    ]
    progress(f"fetching fights for {len(pending)} reports")
    known_encounters = {r["id"]: r["zone_id"] for r in conn.execute("SELECT id, zone_id FROM encounters")}
    for i in range(0, len(pending), 40):
        group = pending[i : i + 40]
        fights_by_code = wcl.report_fights(group)
        with transaction(conn):
            for code in group:
                conn.execute("DELETE FROM fights WHERE report_code = ?", (code,))
                rep_start = conn.execute("SELECT start_time, zone_id FROM reports WHERE code = ?", (code,)).fetchone()
                for f in fights_by_code.get(code, []):
                    enc_id = int(f.get("encounterID") or 0)
                    if enc_id == 0 or enc_id not in known_encounters:
                        continue
                    conn.execute(
                        """INSERT OR REPLACE INTO fights(report_code, fight_id, encounter_id, name, difficulty, kill,
                               start_time, end_time, boss_pct, fight_pct, last_phase, size, avg_ilvl)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            code,
                            int(f["id"]),
                            enc_id,
                            f.get("name"),
                            f.get("difficulty"),
                            1 if f.get("kill") else 0,
                            rep_start["start_time"] + int(f["startTime"]),
                            rep_start["start_time"] + int(f["endTime"]),
                            f.get("bossPercentage"),
                            f.get("fightPercentage"),
                            f.get("lastPhase"),
                            f.get("size"),
                            f.get("averageItemLevel"),
                        ),
                    )
                    stats.fights += 1
                    touched.add(int(known_encounters[enc_id]))
                conn.execute("UPDATE reports SET fights_synced_at = ? WHERE code = ?", (now_ms(), code))
        progress(f"  fights: {min(i + 40, len(pending))}/{len(pending)} reports")
    return sorted(touched)


DUPLICATE_WINDOW_MS = 60_000


def dedupe_fights(conn: sqlite3.Connection, window_ms: int = DUPLICATE_WINDOW_MS) -> int:
    """Flag duplicate pulls: the same boss, at the same difficulty, starting within ``window_ms`` (or overlapping) in a
    *different* report is the same pull logged by a second person. The longer record (or the one that recorded the kill)
    stays canonical. Returns the number of fights marked as duplicates."""
    rows = conn.execute(
        """SELECT report_code, fight_id, encounter_id, difficulty, start_time, end_time, kill, canonical
           FROM fights ORDER BY encounter_id, difficulty, start_time, end_time DESC"""
    ).fetchall()
    flags: dict[tuple[str, int], int] = {}
    kept: dict | None = None
    key_prev = None
    for r in rows:
        key = (r["encounter_id"], r["difficulty"])
        if key != key_prev:
            kept, key_prev = None, key
        cur = dict(r)
        if kept and cur["report_code"] != kept["report_code"] and (
            cur["start_time"] - kept["start_time"] <= window_ms or cur["start_time"] < kept["end_time"]
        ):
            cur_len, kept_len = cur["end_time"] - cur["start_time"], kept["end_time"] - kept["start_time"]
            if (cur["kill"] and not kept["kill"]) or (cur["kill"] == kept["kill"] and cur_len > kept_len):
                flags[(kept["report_code"], kept["fight_id"])] = 0
                flags[(cur["report_code"], cur["fight_id"])] = 1
                kept = cur
            else:
                flags[(cur["report_code"], cur["fight_id"])] = 0
            continue
        flags[(cur["report_code"], cur["fight_id"])] = 1
        kept = cur
    changes = [(flag, code, fid) for (code, fid), flag in flags.items()]
    with transaction(conn):
        conn.executemany("UPDATE fights SET canonical = ? WHERE report_code = ? AND fight_id = ? AND canonical != ?",
                         [(f, c, i, f) for f, c, i in changes])
    return sum(1 for f in flags.values() if f == 0)


def sync_zone_rankings(conn: sqlite3.Connection, wcl: WCLClient, guild_id: int, wcl_guild_id: int, zone_ids: list[int], stats: SyncStats, progress: Progress) -> None:
    for zid in zone_ids:
        try:
            rk = wcl.guild_zone_ranking(wcl_guild_id, zid, difficulty=5)
        except WCLError as exc:
            stats.warn(f"zone ranking unavailable for zone {zid}: {exc}", progress)
            continue
        with transaction(conn):
            # The progress row has a NULL difficulty, which the primary key cannot de-duplicate: clear the zone first.
            conn.execute("DELETE FROM wcl_zone_rankings WHERE guild_id = ? AND zone_id = ?", (guild_id, zid))
            for metric in ("progress", "speed", "completeRaidSpeed"):
                pos = rk.get(metric) or {}
                if not pos:
                    continue

                def num(k: str, f: str = "number", _pos: dict = pos):
                    return (_pos.get(k) or {}).get(f)

                conn.execute(
                    """INSERT OR REPLACE INTO wcl_zone_rankings(guild_id, zone_id, metric, difficulty, world_rank, world_pct,
                           region_rank, region_pct, server_rank, server_pct, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id, zid, metric, None if metric == "progress" else 5,
                        num("worldRank"), num("worldRank", "percentile"),
                        num("regionRank"), num("regionRank", "percentile"),
                        num("serverRank"), num("serverRank", "percentile"),
                        now_ms(),
                    ),
                )
        progress(f"zone ranking synced for zone {zid}")


def sync_attendance(conn: sqlite3.Connection, wcl: WCLClient, wcl_guild_id: int, zone_ids: list[int], stats: SyncStats, progress: Progress) -> None:
    known_reports = {r["code"] for r in conn.execute("SELECT code FROM reports")}
    for zid in zone_ids:
        try:
            rows = wcl.all_attendance(wcl_guild_id, zone_id=zid)
        except WCLError as exc:
            stats.warn(f"attendance unavailable for zone {zid}: {exc}", progress)
            continue
        with transaction(conn):
            for rep in rows:
                code = rep.get("code")
                if code not in known_reports:
                    continue
                conn.execute("DELETE FROM attendance WHERE report_code = ?", (code,))
                for p in rep.get("players") or []:
                    conn.execute(
                        "INSERT OR REPLACE INTO attendance(report_code, player_name, player_class, presence) VALUES (?, ?, ?, ?)",
                        (code, p["name"], p.get("type"), int(p.get("presence") or 1)),
                    )
                    stats.attendance_rows += 1
        progress(f"attendance synced for zone {zid}: {len(rows)} reports")


def _fold(name: str) -> str:
    """Case- and accent-insensitive key for player names (Nórmán == norman)."""
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)).casefold()


def assign_teams(conn: sqlite3.Connection, teams: dict[str, list[str]], min_matches: int, progress: Progress) -> dict[str, int]:
    """Assign every report with attendance to the raid team whose roster shows up most (ties and thin matches stay unassigned)."""
    conn.execute("DELETE FROM report_teams")
    if not teams:
        conn.commit()
        return {}
    rosters = {team: {_fold(p) for p in players} for team, players in teams.items()}
    counts: dict[str, int] = {t: 0 for t in teams}
    rows = conn.execute("SELECT report_code, player_name FROM attendance").fetchall()
    present: dict[str, set[str]] = {}
    for r in rows:
        present.setdefault(r["report_code"], set()).add(_fold(r["player_name"]))
    with transaction(conn):
        for code, names in present.items():
            scores = sorted(((len(names & roster), team) for team, roster in rosters.items()), reverse=True)
            best, team = scores[0]
            if best < min_matches or (len(scores) > 1 and scores[1][0] == best):
                continue
            conn.execute("INSERT INTO report_teams(report_code, team, matches) VALUES (?, ?, ?)", (code, team, best))
            counts[team] += 1
    progress("raid teams: " + ", ".join(f"{t} {n} reports" for t, n in counts.items()))
    return counts


def sync_parses(conn: sqlite3.Connection, wcl: WCLClient, zone_ids: list[int], stats: SyncStats, progress: Progress, limit: int = 120) -> None:
    """Fetch Warcraft Logs rankings (parses) for kill reports that have none yet, newest first, within a per-run budget."""
    if not zone_ids:
        return
    placeholders = ",".join("?" * len(zone_ids))
    pending = [
        r["code"]
        for r in conn.execute(
            f"""SELECT r.code FROM reports r
                WHERE r.zone_id IN ({placeholders}) AND r.rankings_synced_at IS NULL
                  AND EXISTS (SELECT 1 FROM fights f WHERE f.report_code = r.code AND f.kill = 1 AND f.canonical = 1 AND f.difficulty IN (3,4,5))
                ORDER BY r.start_time DESC LIMIT ?""",
            (*zone_ids, limit),
        )
    ]
    # Reports with no (canonical) kills never need rankings: mark them done so we don't look again.
    conn.execute(
        f"""UPDATE reports SET rankings_synced_at = ? WHERE zone_id IN ({placeholders}) AND rankings_synced_at IS NULL
            AND fights_synced_at IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM fights f WHERE f.report_code = reports.code AND f.kill = 1 AND f.canonical = 1 AND f.difficulty IN (3,4,5))""",
        (now_ms(), *zone_ids),
    )
    conn.commit()
    if not pending:
        progress("parses: up to date")
        return
    progress(f"fetching parses for {len(pending)} reports")
    done = 0
    for code in pending:
        rows: list[tuple] = []
        try:
            for metric, roles in (("dps", ("tanks", "dps")), ("hps", ("healers",))):
                for fight in wcl.report_rankings(code, metric):
                    if not fight.get("kill"):
                        continue
                    enc = (fight.get("encounter") or {}).get("id")
                    if not enc:
                        continue
                    for role in roles:
                        for ch in ((fight.get("roles") or {}).get(role) or {}).get("characters") or []:
                            rows.append(
                                (code, int(fight["fightID"]), int(enc), fight.get("difficulty"), ch.get("name"),
                                 (ch.get("server") or {}).get("name"), ch.get("class"), ch.get("spec"), role, metric,
                                 ch.get("amount"), ch.get("rankPercent"), ch.get("bracketPercent"))
                            )
        except WCLError as exc:
            stats.warn(f"parses unavailable for report {code}: {exc}", progress)
            if "rate limit" in str(exc).lower():
                break
            continue
        with transaction(conn):
            conn.execute("DELETE FROM parses WHERE report_code = ?", (code,))
            conn.executemany(
                """INSERT OR REPLACE INTO parses(report_code, fight_id, encounter_id, difficulty, player_name, server, player_class, spec,
                       role, metric, amount, rank_percent, bracket_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [r for r in rows if r[4]],
            )
            conn.execute("UPDATE reports SET rankings_synced_at = ? WHERE code = ?", (now_ms(), code))
        stats.parses += len(rows)
        done += 1
        if done % 20 == 0:
            progress(f"  parses: {done}/{len(pending)} reports")
    progress(f"parses synced for {done} reports ({stats.parses} rows)")


CHARACTER_FIELDS = "gear,guild,mythic_plus_scores_by_season:current,mythic_plus_best_runs,raid_progression"


def sync_characters(conn: sqlite3.Connection, rio: RaiderIOClient, settings: Settings, zone_id: int | None,
                    stats: SyncStats, progress: Progress, min_raids: int = 2, max_age_days: int = 3,
                    retry_missing: bool = False) -> None:
    """Fetch Raider.IO profiles (race, spec, gear, portrait) for the current tier's raiders.

    Only people who actually raid: someone with at least ``min_raids`` nights in the tier, or any parse. Profiles are
    refreshed every few days, and a character Raider.IO cannot find is remembered so we stop asking."""
    if zone_id is None:
        return
    home = settings.home_guild
    rows = _rows_sync(
        conn,
        """SELECT player_name, COUNT(DISTINCT raid_date) AS raids FROM v_attendance
           WHERE zone_id = ? AND presence = 1 GROUP BY player_name""",
        (zone_id,),
    )
    parsed = {
        r["player_name"] for r in _rows_sync(
            conn,
            """SELECT DISTINCT player_name FROM v_parses WHERE zone_id = ?
               AND (server IS NULL OR LOWER(REPLACE(server, char(39), '')) = ?)""",
            (zone_id, home.realm_slug.replace("-", " ")),
        )
    }
    wanted = sorted({r["player_name"] for r in rows if r["raids"] >= min_raids} | parsed)
    if not wanted:
        return
    # Guild members are often on another realm; their parses tell us which one.
    alt_realm: dict[str, str] = {}
    for r in _rows_sync(
        conn,
        """SELECT player_name, server, COUNT(*) AS n FROM v_parses WHERE zone_id = ? AND server IS NOT NULL
           GROUP BY player_name, server ORDER BY n DESC""",
        (zone_id,),
    ):
        alt_realm.setdefault(r["player_name"], slugify(r["server"]))
    fresh_after = now_ms() - max_age_days * 86_400_000
    known = {
        r["name"]: r for r in _rows_sync(conn, "SELECT name, fetched_at, missing FROM characters WHERE region = ?", (home.region,))
    }
    todo = [
        n for n in wanted
        if n not in known
        or (known[n]["fetched_at"] < fresh_after and not known[n]["missing"])
        # A character Raider.IO could not find is retried on a full sync, or after a week in case they came back.
        or (known[n]["missing"] and (retry_missing or known[n]["fetched_at"] < now_ms() - 7 * 86_400_000))
    ]
    if not todo:
        progress(f"characters: {len(wanted)} raiders, all profiles fresh")
        return
    progress(f"fetching character profiles for {len(todo)} raiders")
    found = 0
    for name in todo:
        prof, realm_slug = None, home.realm_slug
        for candidate in dict.fromkeys([home.realm_slug, alt_realm.get(name)]):
            if not candidate:
                continue
            try:
                prof = rio.character_profile(home.region, candidate, name, fields=CHARACTER_FIELDS)
                realm_slug = candidate
                break
            except RaiderIOError:
                continue
        if prof is None:
            # Raider.IO does not know this character (renamed, transferred, or an alt we cannot place).
            with transaction(conn):
                conn.execute(
                    """INSERT INTO characters(name, realm_slug, region, missing, fetched_at) VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(name, realm_slug, region) DO UPDATE SET missing = 1, fetched_at = excluded.fetched_at""",
                    (name, home.realm_slug, home.region, now_ms()),
                )
            continue
        seasons = prof.get("mythic_plus_scores_by_season") or []
        scores = (seasons[0].get("scores") if seasons else {}) or {}
        best_role = max(("dps", "healer", "tank"), key=lambda r: scores.get(r) or 0) if scores else None
        runs = [
            {"level": r.get("mythic_level"), "dungeon": r.get("dungeon"), "score": r.get("score")}
            for r in (prof.get("mythic_plus_best_runs") or [])[:3]
        ]
        gear = (prof.get("gear") or {}).get("items") or {}
        slim = {
            slot: {"name": item.get("name"), "item_level": item.get("item_level"), "icon": item.get("icon")}
            for slot, item in gear.items() if isinstance(item, dict) and item.get("name")
        }
        thumb = prof.get("thumbnail_url") or ""
        with transaction(conn):
            conn.execute(
                """INSERT OR REPLACE INTO characters(name, realm_slug, region, class, race, gender, spec, role,
                       item_level, thumbnail_url, portrait_url, profile_url, guild_name, gear, mplus_score, mplus_role,
                       mplus_best, raid_progression, achievement_points, faction, missing, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (prof.get("name") or name, realm_slug, home.region, prof.get("class"), prof.get("race"),
                 prof.get("gender"), prof.get("active_spec_name"), (prof.get("active_spec_role") or "").lower(),
                 (prof.get("gear") or {}).get("item_level_equipped"), thumb,
                 thumb.replace("-avatar.jpg", "-inset.jpg"), prof.get("profile_url"),
                 (prof.get("guild") or {}).get("name"), json.dumps(slim), scores.get("all"), best_role,
                 json.dumps(runs), json.dumps(prof.get("raid_progression") or {}), prof.get("achievement_points"),
                 prof.get("faction"), now_ms()),
            )
        found += 1
    stats.characters = found
    progress(f"character profiles synced: {found} of {len(todo)}")


# ------------------------------------------------------------------------------------ Raider.IO
def _rows_sync(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _store_profile(conn: sqlite3.Connection, ref: GuildRef, prof: dict, *, is_home: bool, is_rival: bool) -> int:
    gid = upsert_guild(conn, ref, faction=prof.get("faction"), is_home=is_home if is_home else None, is_rival=is_rival if is_rival else None)
    ts = now_ms()
    for slug, s in (prof.get("raid_progression") or {}).items():
        conn.execute(
            """INSERT OR REPLACE INTO rio_summary(guild_id, raid_slug, summary, total_bosses, normal_killed, heroic_killed, mythic_killed, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (gid, slug, s.get("summary"), s.get("total_bosses"), s.get("normal_bosses_killed"), s.get("heroic_bosses_killed"), s.get("mythic_bosses_killed"), ts),
        )
    for slug, ranks in (prof.get("raid_rankings") or {}).items():
        for diff_name, code in RIO_DIFFICULTY_TO_CODE.items():
            r = ranks.get(diff_name) or {}
            conn.execute(
                """INSERT INTO rio_rankings(guild_id, raid_slug, difficulty, world_rank, region_rank, realm_rank, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, raid_slug, difficulty) DO UPDATE SET world_rank = excluded.world_rank,
                       region_rank = excluded.region_rank, realm_rank = excluded.realm_rank, fetched_at = excluded.fetched_at""",
                (gid, slug, code, r.get("world"), r.get("region"), r.get("realm"), ts),
            )
    return gid


def _store_ranking_entry(conn: sqlite3.Connection, entry: dict, raid_slug: str, difficulty: int, ts: int) -> int:
    g = entry["guild"]
    ref = GuildRef(g["name"], (g.get("realm") or {}).get("slug", ""), (g.get("region") or {}).get("slug", ""))
    gid = upsert_guild(conn, ref, faction=g.get("faction"), rio_id=g.get("id"))
    conn.execute(
        """INSERT INTO rio_rankings(guild_id, raid_slug, difficulty, world_rank, region_rank, realm_rank, fetched_at)
           VALUES (?, ?, ?, NULL, ?, ?, ?)
           ON CONFLICT(guild_id, raid_slug, difficulty) DO UPDATE SET region_rank = excluded.region_rank,
               realm_rank = excluded.realm_rank, fetched_at = excluded.fetched_at""",
        (gid, raid_slug, difficulty, entry.get("regionRank"), entry.get("rank"), ts),
    )
    pulled = {p["slug"]: p for p in entry.get("encountersPulled") or []}
    defeated = {d["slug"]: d for d in entry.get("encountersDefeated") or []}
    for slug in set(pulled) | set(defeated):
        d, p = defeated.get(slug, {}), pulled.get(slug, {})
        conn.execute(
            """INSERT OR REPLACE INTO rio_progress(guild_id, raid_slug, difficulty, encounter_slug, first_defeated, last_defeated,
                   num_pulls, best_percent, is_defeated, pull_started_at, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                gid, raid_slug, difficulty, slug,
                iso_to_ms(d.get("firstDefeated")), iso_to_ms(d.get("lastDefeated")),
                p.get("numPulls"), p.get("bestPercent"),
                1 if d else int(bool(p.get("isDefeated"))),
                iso_to_ms(p.get("pullStartedAt")), ts,
            ),
        )
    return gid


BASE_SEASON = re.compile(r"^season-[a-z]+-\d+$")


def season_cutoffs(seasons: list[dict], region: str) -> list[dict]:
    """Base seasons with their cut-off: the date after which kills are post-season (no Cutting Edge / Ahead of the
    Curve, Mythic+ over). Raider.IO marks it with a ``-cutoffs`` season variant, or a ``-post`` season starting then;
    without either, the season's own end date is the cut-off."""
    def when(s: dict, key: str) -> int | None:
        d = s.get(key) or {}
        return iso_to_ms(d.get(region) or d.get("us"))

    by_slug = {s.get("slug"): s for s in seasons if s.get("slug")}
    out = []
    for slug, s in by_slug.items():
        if not BASE_SEASON.match(slug):
            continue
        starts, ends = when(s, "starts"), when(s, "ends")
        candidates = [c for c in (ends, when(by_slug.get(f"{slug}-cutoffs", {}), "ends"),
                                  when(by_slug.get(f"{slug}-post", {}), "starts")) if c]
        out.append({"slug": slug, "starts": starts, "ends": ends, "cutoff": min(candidates) if candidates else None})
    return [s for s in out if s["starts"]]


def _apply_raid_windows(conn: sqlite3.Connection, raids: list[dict], seasons: list[dict], region: str, exp_id: int,
                        progress: Progress) -> None:
    """Store each raid's open/close dates and the cut-off of the season it belongs to (nearest season start)."""
    cutoffs = season_cutoffs(seasons, region)
    for raid in raids:
        starts = iso_to_ms((raid.get("starts") or {}).get(region) or (raid.get("starts") or {}).get("us"))
        ends = iso_to_ms((raid.get("ends") or {}).get(region) or (raid.get("ends") or {}).get("us"))
        season = min(cutoffs, key=lambda s: abs(s["starts"] - starts)) if (cutoffs and starts) else None
        cutoff = season["cutoff"] if season else None
        # A tier also ends when the next raid opens, whichever comes first.
        cutoff = min([c for c in (cutoff, ends) if c], default=None)
        conn.execute(
            "UPDATE rio_raids SET starts_at = ?, ends_at = ?, season_slug = ?, cutoff_at = ? WHERE slug = ?",
            (starts, ends, season["slug"] if season else None, cutoff, raid["slug"]),
        )
    named = [r["slug"] for r in raids if r.get("starts")]
    if named:
        progress(f"raider.io season windows for expansion {exp_id}: {len(named)} raids")


def _load_rio_static(conn: sqlite3.Connection, rio: RaiderIOClient, wanted_slugs: set[str], stats: SyncStats, progress: Progress,
                     n_expansions: int = 2, region: str = "eu") -> None:
    """Fetch Raider.IO raid/boss reference data for the newest ``n_expansions`` expansions (and any expansion still
    holding a wanted slug), so older tiers we have logs for can be linked to Raider.IO as well."""
    known = {r["slug"] for r in conn.execute("SELECT slug FROM rio_raids")}
    missing = wanted_slugs - known
    loaded_expansions = {r["expansion_id"] for r in conn.execute(
        "SELECT DISTINCT expansion_id FROM rio_raids WHERE expansion_id IS NOT NULL AND cutoff_at IS NOT NULL")}
    wanted_expansions = max(1, n_expansions)
    if not missing and len(loaded_expansions) >= wanted_expansions:
        return
    seen = 0
    for exp_id in range(13, 7, -1):
        try:
            data = rio.static_data(exp_id)
        except RaiderIOError:
            continue
        raids = data.get("raids") or []
        if not raids:
            continue
        with transaction(conn):
            for i, raid in enumerate(raids):
                conn.execute(
                    "INSERT OR REPLACE INTO rio_raids(slug, name, expansion_id, ord) VALUES (?, ?, ?, ?)",
                    (raid["slug"], raid["name"], exp_id, raid.get("id") or i),
                )
                for j, enc in enumerate(raid.get("encounters") or []):
                    conn.execute(
                        "INSERT OR REPLACE INTO rio_encounters(raid_slug, slug, name, ord) VALUES (?, ?, ?, ?)",
                        (raid["slug"], enc["slug"], enc["name"], enc.get("ordinal", j) if isinstance(enc.get("ordinal"), int) else j + 1),
                    )
                missing.discard(raid["slug"])
            try:
                seasons = (rio.mythic_plus_static_data(exp_id) or {}).get("seasons") or []
            except RaiderIOError as exc:
                stats.warn(f"raider.io season data for expansion {exp_id} failed: {exc}", progress)
                seasons = []
            _apply_raid_windows(conn, raids, seasons, region, exp_id, progress)
        seen += 1
        progress(f"raider.io static data for expansion {exp_id}: {len(raids)} raids")
        if not missing and seen >= wanted_expansions:
            break
    if missing:
        stats.warn(f"raider.io static data not found for raids: {sorted(missing)}", progress)


def map_zones_to_rio(conn: sqlite3.Connection, tier_map: dict[int, str], stats: SyncStats, progress: Progress) -> None:
    """Link Warcraft Logs zones/encounters to Raider.IO raids/bosses by name (with manual overrides)."""
    raids = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM rio_raids")}
    if not raids:
        return
    with transaction(conn):
        for z in conn.execute("SELECT id, name FROM zones").fetchall():
            slug = tier_map.get(int(z["id"])) or match_name(z["name"], raids, cutoff=0.85)
            if not slug:
                continue
            conn.execute("UPDATE zones SET rio_raid_slug = ? WHERE id = ?", (slug, z["id"]))
            bosses = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM rio_encounters WHERE raid_slug = ?", (slug,))}
            for e in conn.execute("SELECT id, name FROM encounters WHERE zone_id = ?", (z["id"],)).fetchall():
                bslug = match_name(e["name"], bosses, cutoff=0.7)
                if bslug:
                    conn.execute("UPDATE encounters SET rio_encounter_slug = ? WHERE id = ?", (bslug, e["id"]))
                else:
                    stats.warn(f"no raider.io boss match for '{e['name']}' in {z['name']}", progress)
    mapped = conn.execute("SELECT COUNT(*) AS c FROM zones WHERE rio_raid_slug IS NOT NULL").fetchone()["c"]
    progress(f"zone mapping: {mapped} zones linked to raider.io raids")


def sync_raiderio(conn: sqlite3.Connection, rio: RaiderIOClient, settings: Settings, stats: SyncStats, progress: Progress) -> None:
    home = settings.home_guild
    try:
        prof = rio.guild_profile(home.region, home.realm_slug, home.name)
    except RaiderIOError as exc:
        stats.warn(f"raider.io profile for {home.name} failed: {exc}", progress)
        return
    with transaction(conn):
        home_id = _store_profile(conn, home, prof, is_home=True, is_rival=False)
    raid_slugs = set((prof.get("raid_progression") or {}).keys())
    progress(f"raider.io profile: {home.name} ({prof.get('faction')}) raids: {sorted(raid_slugs)}")

    rival_realms: set[tuple[str, str]] = set()
    with transaction(conn):
        conn.execute("UPDATE guilds SET is_rival = 0")
    for rival in settings.rivals:
        try:
            rp = rio.guild_profile(rival.region, rival.realm_slug, rival.name)
        except RaiderIOError as exc:
            stats.warn(f"raider.io profile for rival {rival.name} failed: {exc}", progress)
            continue
        with transaction(conn):
            _store_profile(conn, rival, rp, is_home=False, is_rival=True)
        raid_slugs |= set((rp.get("raid_progression") or {}).keys())
        if (rival.realm_slug, rival.region) != (home.realm_slug, home.region):
            rival_realms.add((rival.realm_slug, rival.region))
        stats.rio_guilds += 1

    _load_rio_static(conn, rio, raid_slugs, stats, progress, n_expansions=settings.sync_expansions, region=home.region)
    map_zones_to_rio(conn, settings.tier_map_dict, stats, progress)

    # Also cover every older raid we have logs for: Raider.IO is the record of what the guild actually killed, and
    # guild-tagged logs can miss kills (a night logged personally, or not uploaded at all).
    raid_slugs |= {
        r["rio_raid_slug"]
        for r in conn.execute(
            """SELECT DISTINCT z.rio_raid_slug FROM zones z WHERE z.rio_raid_slug IS NOT NULL
               AND EXISTS (SELECT 1 FROM reports r WHERE r.zone_id = z.id)"""
        )
    }
    # Only scan real raids (skip 1-boss world-boss "raids") to save requests.
    placeholders = ",".join("?" * len(raid_slugs))
    scan_raids = [
        r["slug"]
        for r in conn.execute(
            f"SELECT slug FROM rio_raids WHERE slug IN ({placeholders}) "
            "AND (SELECT COUNT(*) FROM rio_encounters e WHERE e.raid_slug = rio_raids.slug) > 1 ORDER BY ord DESC",
            tuple(raid_slugs),
        )
    ] if raid_slugs else []
    ts = now_ms()
    realms = [(home.realm_slug, home.region)] + sorted(rival_realms)
    for realm_slug, region in realms:
        pages = settings.rio_realm_scan_pages if (realm_slug, region) == (home.realm_slug, home.region) else 1
        for raid_slug in scan_raids:
            for diff_name, code in RIO_DIFFICULTY_TO_CODE.items():
                seen_home = False
                for page in range(pages):
                    try:
                        entries = rio.raid_rankings(raid_slug, diff_name, region, realm=realm_slug, page=page)
                    except RaiderIOError as exc:
                        stats.warn(f"raid rankings {raid_slug}/{diff_name} p{page} failed: {exc}", progress)
                        break
                    with transaction(conn):
                        for entry in entries:
                            gid = _store_ranking_entry(conn, entry, raid_slug, code, ts)
                            stats.rio_progress_rows += len(entry.get("encountersPulled") or []) + len(entry.get("encountersDefeated") or [])
                            if gid == home_id:
                                seen_home = True
                    if len(entries) < 100:
                        break
                # Make sure the home guild's own per-boss data is present even if it is deep in the standings.
                if (realm_slug, region) == (home.realm_slug, home.region) and not seen_home:
                    rank_row = conn.execute(
                        "SELECT realm_rank FROM rio_rankings WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?",
                        (home_id, raid_slug, code),
                    ).fetchone()
                    if rank_row and rank_row["realm_rank"]:
                        page = (int(rank_row["realm_rank"]) - 1) // 100
                        if page >= pages:
                            try:
                                entries = rio.raid_rankings(raid_slug, diff_name, region, realm=realm_slug, page=page)
                                with transaction(conn):
                                    for entry in entries:
                                        if entry["guild"]["name"].lower() == home.name.lower():
                                            _store_ranking_entry(conn, entry, raid_slug, code, ts)
                            except RaiderIOError as exc:
                                stats.warn(f"could not fetch home guild page for {raid_slug}/{diff_name}: {exc}", progress)
            progress(f"raider.io realm standings synced: {realm_slug}/{region} {raid_slug}")
    stats.rio_guilds = conn.execute("SELECT COUNT(*) AS c FROM guilds").fetchone()["c"]


# ------------------------------------------------------------------------------------- orchestrate
def run_sync(
    conn: sqlite3.Connection,
    settings: Settings,
    wcl: WCLClient | None,
    rio: RaiderIOClient | None,
    *,
    full: bool = False,
    skip_attendance: bool = False,
    progress: Progress | None = None,
) -> SyncStats:
    progress = progress or (lambda msg: log.info(msg))
    stats = SyncStats()
    started = now_ms()
    cur = conn.execute("INSERT INTO sync_log(started_at, status) VALUES (?, 'running')", (started,))
    log_id = cur.lastrowid
    conn.commit()
    status = "ok"
    try:
        if wcl is not None:
            progress("== Warcraft Logs ==")
            sync_zones(conn, wcl, settings.sync_expansions, stats, progress)
            gid = sync_home_guild_wcl(conn, wcl, settings, stats, progress)
            wcl_gid = conn.execute("SELECT wcl_id FROM guilds WHERE id = ?", (gid,)).fetchone()["wcl_id"]
            touched = sync_reports(conn, wcl, gid, wcl_gid, full, stats, progress)
            dups = dedupe_fights(conn)
            if dups:
                progress(f"duplicate pulls from second loggers hidden: {dups}")
            active_zones = [
                r["zone_id"]
                for r in conn.execute(
                    "SELECT DISTINCT zone_id FROM reports WHERE guild_id = ? ORDER BY zone_id DESC LIMIT 8", (gid,)
                )
            ]
            rank_zones = sorted(set(active_zones[:4]) | set(touched))[-4:]
            sync_zone_rankings(conn, wcl, gid, wcl_gid, rank_zones, stats, progress)
            if not skip_attendance:
                # Attendance drives the raid-team split, so cover every tier we have logs for (newest first).
                sync_attendance(conn, wcl, wcl_gid, active_zones, stats, progress)
            assign_teams(conn, settings.teams, settings.raid_team_min_matches, progress)
            if settings.sync_parses_per_run > 0:
                sync_parses(conn, wcl, active_zones[:4], stats, progress, limit=settings.sync_parses_per_run)
            stats.wcl_queries = wcl.queries_made
            try:
                rl = wcl.rate_limit()
                set_meta(conn, "wcl_rate_limit", json.dumps(rl))
                progress(f"WCL points used this hour: {rl.get('pointsSpentThisHour'):.0f}/{rl.get('limitPerHour')}")
            except WCLError:
                pass
        else:
            upsert_guild(conn, settings.home_guild, is_home=True)
            conn.commit()
            progress("Warcraft Logs credentials not set - skipping WCL sync (see docs/SETUP.md)")

        if rio is not None:
            progress("== Raider.IO ==")
            sync_raiderio(conn, rio, settings, stats, progress)
            newest = conn.execute(
                """SELECT z.id FROM zones z WHERE EXISTS (SELECT 1 FROM reports r WHERE r.zone_id = z.id)
                   ORDER BY z.id DESC LIMIT 1""").fetchone()
            sync_characters(conn, rio, settings, newest["id"] if newest else None, stats, progress, retry_missing=full)
            stats.rio_requests = rio.requests_made
        set_meta(conn, "last_sync", str(now_ms()))
        conn.commit()
        from .state import configured, persist, publish_page

        if configured(settings):
            progress("snapshot uploaded" if persist(settings) else "warning: snapshot upload failed")
            try:
                from .public import render_public_page

                html = render_public_page(conn, settings)
            except Exception as exc:  # noqa: BLE001 - the public page must never fail the sync
                stats.warn(f"public site render failed: {exc}", progress)
            else:
                progress("public site published" if publish_page(settings, html) else "warning: public site publish failed")
    except Exception as exc:  # noqa: BLE001 - we want the log row to capture any failure
        status = "error"
        stats.warn(f"sync failed: {exc}", progress)
        raise
    finally:
        conn.execute(
            "UPDATE sync_log SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (now_ms(), status, json.dumps({k: v for k, v in stats.__dict__.items() if k != "warnings"} | {"warnings": stats.warnings[:50]}), log_id),
        )
        conn.commit()
    return stats
