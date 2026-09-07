"""The public guild site (killingtime.fyi): a single self-contained HTML page with the latest progress.

It is rendered from the database after every sync and pushed to the Worker (``state.publish_page``), which serves
it at the apex domain without waking the container. The same page is available live at ``/public``."""

from __future__ import annotations

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
    history = [tier_block(t) for t in raid_tiers[1:6]]
    return {
        "guild": ov["guild"],
        "settings": {
            "name": settings.guild_name, "realm": settings.guild_realm, "region": settings.guild_region.upper(),
            "tagline": settings.site_tagline, "about": settings.site_about, "raid_times": settings.site_raid_times,
            "recruiting": settings.site_recruiting, "site_url": settings.site_url,
        },
        "links": guild_links(conn, settings),
        "teams": teams,
        "current": current,
        "history": history,
        "latest_kills": metrics.latest_kills(conn, limit=12),
        "raiderio": ov["raiderio"],
        "last_sync": ov["last_sync"],
        "last_sync_ms": ov["last_sync_ms"],
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "version": __version__,
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
                "bio": c["bio"][:5],   # the public page wants a paragraph, not a dossier
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


def render_public_team_page(conn: sqlite3.Connection, settings: Settings) -> str:
    """The public Meet the Team page (killingtime.fyi/team)."""
    data = public_summary(conn, settings)
    return _env.get_template("public_team.html").render(page="team", groups=roster_cards(conn, settings), **data)
