"""The public guild site (killingtime.fyi): a single self-contained HTML page with the latest progress.

It is rendered from the database after every sync and pushed to the Worker (``state.publish_page``), which serves
it at the apex domain without waking the container. The same page is available live at ``/public``."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__, metrics
from .config import Settings
from .db import DIFFICULTIES

TEMPLATES = Path(__file__).parent / "web" / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))
_env.filters["num"] = lambda v, d=0: ("-" if v is None else f"{v:,.{d}f}")
_env.globals["DIFFICULTIES"] = DIFFICULTIES


def guild_links(conn: sqlite3.Connection, settings: Settings) -> dict[str, str]:
    home = metrics.home_guild(conn)
    realm = settings.home_guild.realm_slug
    region = settings.guild_region.lower()
    links = {
        "raiderio": f"https://raider.io/guilds/{region}/{realm}/{quote(settings.guild_name)}",
        "warcraftlogs": (
            f"https://www.warcraftlogs.com/guild/id/{home['wcl_id']}"
            if home and home.get("wcl_id")
            else f"https://www.warcraftlogs.com/guild/{region}/{realm}/{quote(settings.guild_name)}"
        ),
        "armory": f"https://worldofwarcraft.blizzard.com/en-gb/guild/{region}/{realm}/{quote(settings.guild_name.lower().replace(' ', '-'))}",
        "progress": settings.kt_state_url or "",
        "apply": settings.site_apply_url,
        "discord": settings.site_discord_url,
    }
    return {k: v for k, v in links.items() if v}


def public_summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Everything the public page shows, as plain data (also served at /api/public)."""
    ov = metrics.overview(conn)
    teams = [t for t in settings.team_names if t in ov["teams"]] or ov["teams"]
    raid_tiers = [t for t in ov["tiers"] if t["bosses"] > 1]  # skip world-boss zones
    rio_by_slug = {r["raid_slug"]: r for r in ov["raiderio"]}

    def ranks(t: dict) -> dict[str, Any]:
        slug = t.get("rio_raid_slug") or ""
        r = rio_by_slug.get(slug)
        out: dict[str, Any] = {}
        if r:
            if r["mythic_world"]:
                out["mythic"] = {"world": r["mythic_world"], "realm": r["mythic_realm"]}
            if r["heroic_world"]:
                out["heroic"] = {"world": r["heroic_world"], "realm": r["heroic_realm"]}
        # Older tiers are not in the guild profile (Raider.IO only reports the current expansion there), but the
        # realm leaderboard still has our rank.
        for row in conn.execute(
            """SELECT k.difficulty, k.realm_rank, k.world_rank FROM rio_rankings k JOIN guilds g ON g.id = k.guild_id
               WHERE g.is_home = 1 AND k.raid_slug = ? AND k.difficulty IN (4, 5) AND k.realm_rank > 0""", (slug,)
        ):
            key = "mythic" if row["difficulty"] == 5 else "heroic"
            if key not in out:
                out[key] = {"world": row["world_rank"], "realm": row["realm_rank"]}
        wcl = conn.execute(
            "SELECT world_rank, region_rank, server_rank FROM wcl_zone_rankings WHERE zone_id = ? AND metric = 'progress'", (t["id"],)
        ).fetchone()
        if wcl and wcl["world_rank"]:
            out["wcl"] = {"world": wcl["world_rank"], "region": wcl["region_rank"], "realm": wcl["server_rank"]}
        return out

    def tier_block(t: dict) -> dict[str, Any]:
        diffs = []
        for d in metrics.RAID_DIFFS:
            s = metrics.tier_summary(conn, t["id"], d)
            if s["pulls"] == 0:
                continue
            diffs.append(
                {
                    "difficulty": d, "name": DIFFICULTIES[d], "killed": s["killed"], "total": s["total_bosses"],
                    "cleared": s["cleared"], "pulls": s["pulls"], "nights": s["nights"],
                    "achievement": s["achievement"], "achievement_earned": s["achievement_earned"],
                    "tier_over": s["tier_over"], "cutoff_date": s["cutoff_date"],
                    "post_season": len(s["post_season_kills"]), "killed_all_time": s["killed_all_time"],
                    "next_boss": s["next_boss"]["name"] if s["next_boss"] else None,
                    "next_best_pct": round(s["next_boss"]["best_pct"], 1) if s["next_boss"] and s["next_boss"]["best_pct"] is not None else None,
                    "first_pull_date": s["first_pull_date"], "last_kill_date": s["last_kill_date"],
                }
            )
        return {
            "id": t["id"], "name": t["name"], "expansion": t["expansion"], "bosses": t["bosses"],
            "first": t["first_report_date"], "last": t["last_report_date"],
            "difficulties": diffs, "ranks": ranks(t), "summary": t["summary"],
            "teams": metrics.team_progress(conn, t["id"], teams) if teams else [],
        }

    current = tier_block(raid_tiers[0]) if raid_tiers else None
    schedule = stated_hours(metrics.raid_schedule(conn), settings.site_raid_hours)
    strip = [
        {"player": m["player"], "slug": m["slug"], "portrait": m["portrait"], "spec": m["spec"], "class": m["class"]}
        for g in roster_cards(conn, settings) for m in g["members"] if m["generated"]
    ]
    history = [tier_block(t) for t in raid_tiers[1:10]]
    return {
        "guild": ov["guild"],
        "settings": {
            "name": settings.guild_name, "realm": settings.guild_realm, "region": settings.guild_region.upper(),
            "tagline": settings.site_tagline, "about": settings.site_about, "raid_times": settings.site_raid_times,
            "recruiting": settings.site_recruiting, "site_url": settings.site_url,
        },
        "links": guild_links(conn, settings),
        "teams": teams,
        "schedule": schedule,
        "clips": site_clips(),
        "kill_reel": kill_reel(conn, settings),
        "latest_kill": latest_kill(conn),
        "portrait_strip": strip,
        "recruiting": recruiting_block(conn, settings, current, schedule, strip),
        "totals": guild_totals(conn),
        "current": current,
        "history": history,
        "latest_kills": metrics.latest_kills(conn, limit=12),
        "raiderio": ov["raiderio"],
        "last_sync": ov["last_sync"],
        "last_sync_ms": ov["last_sync_ms"],
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "version": __version__,
    }


# The generic fight reel, shown only when we have no artwork for the bosses we actually killed. Captions stay bare:
# these are stock scenes, so a caption with a pull count in it would be inventing one.
CLIPS = [
    {"file": "pull", "title": "The pull", "sub": "", "wide": True},
    {"file": "kill", "title": "A kill", "sub": "", "wide": True},
    {"file": "charge", "title": "The charge", "sub": ""},
    {"file": "heal", "title": "Healing through it", "sub": ""},
    {"file": "wipe", "title": "A wipe", "sub": ""},
]


def site_clips() -> list[dict[str, Any]]:
    """Only offer a clip whose poster actually exists on disk."""
    static = Path(__file__).parent / "web" / "static" / "site"
    return [c for c in CLIPS if (static / f"{c['file']}.webp").exists()]


def boss_art_dir() -> Path:
    """Where the generated boss artwork lives. A function so a test can point it somewhere disposable: writing
    fixtures into the shipped asset directory once deleted a boss's picture."""
    return Path(__file__).parent / "web" / "static" / "site" / "bosses"


def boss_art(slug: str) -> dict[str, str | None]:
    """The generated artwork for one boss, if it has been made yet: still and clip are independent."""
    static = boss_art_dir()
    if not slug:
        return {"still": None, "video": None}
    out: dict[str, str | None] = {}
    for key, ext in (("still", "webp"), ("video", "mp4")):
        out[key] = f"/static/site/bosses/{slug}.{ext}" if (static / f"{slug}.{ext}").exists() else None
    return out


def _reel_entry(boss: str, tier: str, difficulty: str, date: str | None, pulls: int | None,
                ce: bool, final: bool) -> dict[str, Any]:
    """One kill as the gallery wants it. Every word of ``sub`` comes from the logs; nothing is dressed up."""
    slug = metrics._slug(boss)
    art = boss_art(slug)
    bits = ["Cutting Edge" if ce else difficulty]
    if pulls:
        bits.append(f"{pulls} pull{'' if pulls == 1 else 's'}")
    if date:
        bits.append(date)
    return {
        "boss": boss, "slug": slug, "tier": tier, "difficulty": difficulty, "date": date,
        "pulls": int(pulls) if pulls else None, "ce": ce, "final": final,
        "still": art["still"], "video": art["video"],
        "headline": "Cutting Edge" if ce else "Final boss down" if final else f"{difficulty} kill",
        "sub": " · ".join(bits),
    }


def kill_reel(conn: sqlite3.Connection, settings: Settings, limit: int = 8) -> list[dict[str, Any]]:
    """The kills worth a picture, best first: Cutting Edge, then other Mythic end bosses, then the current tier."""
    ce_finals: list[tuple[int, dict]] = []
    other_finals: list[tuple[int, dict]] = []
    recent: list[tuple[int, dict]] = []
    # A world-boss zone has no Raider.IO raid behind it; a one-boss raid like Sporefall does, and its Cutting Edge
    # counts as much as any other, so filter on the mapping rather than on the boss count.
    raid_tiers = [t for t in metrics.tiers(conn) if t["bosses"] > 1 or t.get("rio_raid_slug")]
    for i, t in enumerate(raid_tiers):
        s = metrics.tier_summary(conn, t["id"], 5)
        if not s["bosses"]:
            continue
        # Cutting Edge is only a fact once the season has closed; on a live tier (or one whose season dates we
        # never got) a dead end boss is just a dead end boss.
        ce = bool(s["tier_over"] and s["achievement_earned"])
        tier_name = (s["zone"] or {}).get("name") or t["name"]
        for b in s["bosses"]:
            if not b["killed_any"] or not b["kill_ms"]:
                continue
            final = b is s["bosses"][-1]
            entry = _reel_entry(b["name"], tier_name, DIFFICULTIES[5], b["kill_date"], b["pulls_to_kill"],
                                ce=ce and final, final=final)
            if final and ce:
                ce_finals.append((b["kill_ms"], entry))
            elif final:
                other_finals.append((b["kill_ms"], entry))
            elif i == 0:  # the tier we are on now: its ordinary kills are still news
                recent.append((b["kill_ms"], entry))

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in (ce_finals, other_finals, recent):
        for _, entry in sorted(group, key=lambda kv: kv[0], reverse=True):
            # No artwork, no entry: a boss shown under someone else's dragon is worse than no picture at all.
            if not entry["still"] or entry["slug"] in seen:
                continue
            seen.add(entry["slug"])
            out.append(entry)
    return out[:limit]


def latest_kill(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recent first kill, shaped like a reel entry so the page can caption it with matching art."""
    rows = metrics.latest_kills(conn, limit=1)
    if not rows:
        return None
    k = rows[0]
    last_ord = conn.execute("SELECT MAX(ord) AS ord FROM encounters WHERE zone_id = ?", (k["zone_id"],)).fetchone()
    final = bool(last_ord and last_ord["ord"] is not None and k["encounter_ord"] == last_ord["ord"])
    ce = False
    if final and k["difficulty"] == 5:
        s = metrics.tier_summary(conn, k["zone_id"], 5)
        ce = bool(s["tier_over"] and s["achievement_earned"])
    return _reel_entry(k["boss"], k["zone_name"], k["difficulty_name"], k["date"], k["pulls"], ce=ce, final=final)


def stated_hours(schedule: dict[str, Any], stated: str) -> dict[str, Any]:
    """Replace the times worked out from the logs with the window the guild advertises.

    Which nights we raid is a fact the logs know better than anyone. What time we raid is a decision, and the logs
    only see when the first pull happened to go out."""
    m = re.match(r"^\s*(\d{1,2}:\d{2})\s*(?:-|to|\u2013|\u2014)\s*(\d{1,2}:\d{2})\s*$", stated or "")
    if not m or not schedule.get("days"):
        return schedule
    start, end = m.group(1), m.group(2)
    minutes = lambda t: int(t[:2]) * 60 + int(t[3:])  # noqa: E731
    span = (minutes(end) - minutes(start)) % (24 * 60) or 24 * 60
    days = [{**d, "start": start, "end": end} for d in schedule["days"]]
    return {**schedule, "days": days, "stated": True,
            "hours_per_week": round(len(days) * span / 60.0, 1),
            "summary": ", ".join(d["short"] for d in days) + f" · {start}-{end} server time"}


def guild_totals(conn: sqlite3.Connection) -> dict[str, Any]:
    """The headline numbers for the landing page, all of them from our own logs."""
    row = conn.execute(
        """SELECT COUNT(*) AS pulls, COUNT(DISTINCT pull_date) AS nights, SUM(kill) AS kills,
                  SUM(duration_s) / 3600.0 AS hours, MIN(start_time) AS since_ms
           FROM v_pulls"""
    ).fetchone()
    raiders = conn.execute("SELECT COUNT(DISTINCT player_name) AS n FROM v_attendance WHERE presence = 1").fetchone()
    tiers = conn.execute(
        "SELECT COUNT(*) AS n FROM zones z WHERE EXISTS (SELECT 1 FROM reports r WHERE r.zone_id = z.id)").fetchone()
    since = metrics.ms_to_date(row["since_ms"]) if row and row["since_ms"] else None
    years = None
    if row and row["since_ms"]:
        years = round((datetime.now(UTC).timestamp() * 1000 - row["since_ms"]) / (365.25 * 24 * 3600 * 1000), 1)
    return {
        "pulls": row["pulls"] if row else 0,
        "nights": row["nights"] if row else 0,
        "kills": row["kills"] if row else 0,
        "hours": round(row["hours"] or 0) if row else 0,
        "raiders": raiders["n"] if raiders else 0,
        "tiers": tiers["n"] if tiers else 0,
        "since": since,
        "years": years,
    }


ROLE_LABELS = {"tanks": "Tanks", "healers": "Healers", "dps": "DPS"}


def recruiting_block(conn: sqlite3.Connection, settings: Settings, current: dict | None,
                     schedule: dict, strip: list[dict]) -> dict[str, Any]:
    """What a prospective raider needs to know, assembled from the logs where we can and settings where we cannot."""
    comp = {"tanks": 0, "healers": 0, "dps": 0}
    for g in roster_cards(conn, settings):
        for m in g["members"]:
            if m.get("role") in comp:
                comp[m["role"]] += 1
    return {
        "open": settings.site_recruiting,          # free text: "Recruiting: 1 healer, ranged DPS"
        "apply_url": settings.site_apply_url,
        "discord_url": settings.site_discord_url,
        "schedule": schedule,
        "clips": site_clips(),
        "composition": [{"role": k, "label": ROLE_LABELS[k], "count": v} for k, v in comp.items()],
        "tier": current["name"] if current else None,
        "progress": (current["difficulties"][0] if current and current["difficulties"] else None),
        "portraits": strip[:12],
    }


def _frames_on_disk(settings: Settings, slug: str, count: int) -> list[str]:
    """Only offer portrait frames that actually exist, so the public page never shows a broken image."""
    url = settings.member_image_url
    if not url.startswith("/static/"):
        return []  # served from somewhere we cannot check; the page falls back to the Blizzard render
    static = Path(__file__).parent / "web" / "static"
    out = []
    for n in range(1, count + 1):
        rel = url.format(slug=slug, n=n)[len("/static/"):]
        if (static / rel).exists():
            out.append(url.format(slug=slug, n=n))
        else:
            break  # frames are only useful as a complete run from 1
    return out


def roster_cards(conn: sqlite3.Connection, settings: Settings) -> list[dict[str, Any]]:
    """Meet the Team for the public site: one group per raid team, each with its raiders' cards.

    Same numbers as the internal page, minus anything that is nobody else's business: no prompts, no per-boss
    log links. A raider appears under the team their attendance puts them in."""
    cur = metrics.current_tier(conn)
    if not cur:
        return []
    zone_id = cur["id"]
    ov = metrics.overview(conn)
    teams = [t for t in settings.team_names if t in ov["teams"]] or ov["teams"]
    groups = []
    for team in [*teams, None] if teams else [None]:
        diff = metrics.best_difficulty(conn, zone_id, team)
        cards = metrics.meet_the_team(conn, zone_id, diff, team, min_raids=2, alts=settings.alts,
                                      image_url=settings.member_image_url)
        seen = {c["player"] for g in groups for c in g["members"]}
        # The configured roster leads the group; everyone else who raids with that team follows, by attendance.
        named = {metrics._slug(n): i for i, n in enumerate(settings.teams.get(team, []))} if team else {}
        cards.sort(key=lambda c: (named.get(c["slug"], len(named)), -(c["pct"] or 0)))
        members = []
        for c in cards:
            if c["player"] in seen:
                continue  # already shown under their own team; the guild group is the leftovers
            frames = _frames_on_disk(settings, c["slug"], len(c["frames"]))
            members.append({
                "player": c["player"], "slug": c["slug"], "team": team,
                "race": c["race"], "gender": c["gender"], "class": c["class"], "spec": c["spec"], "role": c["role"],
                "item_level": round(c["item_level"]) if c["item_level"] else None,
                "portrait": frames[0] if frames else (c["portrait_url"] or c["thumbnail_url"]),
                "frames": frames,
                "generated": bool(frames),
                "profile_url": c["profile_url"],
                "weapons": c["weapons"],
                "story": c.get("story"),
                "bio": c["bio"][:5],   # the fallback when nobody has written them a story yet
                "stats": c["stats"],
                "pct": c["pct"], "raids": c["raids"], "avg": c["avg"], "best": c["best"],
                "mplus_score": round(c["mplus_score"]) if c["mplus_score"] else None,
                "tiers": (c["history"] or {}).get("tier_count") or 0,
                "career_raids": (c["history"] or {}).get("total_raids") or 0,
                "since": (c["history"] or {}).get("since"),
                "alts": c["alts"],
            })
        if members:
            groups.append({"team": team or "Also raiding with us", "difficulty": DIFFICULTIES.get(diff, ""),
                           "is_team": bool(team), "members": members})
    return groups


def render_public_page(conn: sqlite3.Connection, settings: Settings) -> str:
    data = public_summary(conn, settings)
    return _env.get_template("public.html").render(page="home", **data)


def render_public_join_page(conn: sqlite3.Connection, settings: Settings) -> str:
    """The recruitment page (killingtime.fyi/join)."""
    data = public_summary(conn, settings)
    return _env.get_template("public_join.html").render(page="join", **data)


def render_public_team_page(conn: sqlite3.Connection, settings: Settings) -> str:
    """The public Meet the Team page (killingtime.fyi/team)."""
    data = public_summary(conn, settings)
    return _env.get_template("public_team.html").render(page="team", groups=roster_cards(conn, settings), **data)
