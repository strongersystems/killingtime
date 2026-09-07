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


def render_public_page(conn: sqlite3.Connection, settings: Settings) -> str:
    data = public_summary(conn, settings)
    return _env.get_template("public.html").render(**data)
