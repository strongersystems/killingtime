"""FastAPI web app.

Navigation is team-first: ``/`` asks "which team?" (remembered in a cookie), then everything lives under
``/t/<team>/`` - Progress (the current tier by default, with tier/difficulty switching), Roster, Nights, History,
Peers and Realm. ``guild`` is the whole-guild view. Ask and Status are guild-wide utilities."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .. import __version__, metrics
from ..config import Settings, get_settings
from ..db import DIFFICULTIES, connect

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
GUILD = "guild"
TEAM_COOKIE = "kt_team"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "team"


class SyncManager:
    """Runs at most one sync at a time in a background thread and keeps a rolling log."""

    def __init__(self, settings: Settings, db_path: str) -> None:
        self.settings = settings
        self.db_path = db_path
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.last_result: str | None = None
        self.started_at: float | None = None

    def start(self, full: bool = False) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.log = []
            self.started_at = time.time()
        threading.Thread(target=self._run, args=(full,), daemon=True, name="kt-sync").start()
        return True

    def _run(self, full: bool) -> None:
        from ..raiderio import RaiderIOClient
        from ..sync import run_sync
        from ..wcl import WCLClient

        conn = connect(self.db_path)
        try:
            wcl = None
            if self.settings.wcl_configured:
                wcl = WCLClient(
                    self.settings.wcl_client_id, self.settings.wcl_client_secret, self.settings.wcl_token_url,
                    self.settings.wcl_api_url, self.settings.wcl_token_cache,
                )
            stats = run_sync(conn, self.settings, wcl, RaiderIOClient(self.settings.rio_api_url), full=full, progress=self._log)
            self.last_result = f"ok: {stats.reports} reports, {stats.fights} pulls, {stats.parses} parses, {stats.rio_progress_rows} raider.io rows, {len(stats.warnings)} warnings"
        except Exception as exc:  # noqa: BLE001
            self.last_result = f"error: {exc}"
            self._log(self.last_result)
        finally:
            conn.close()
            self.running = False

    def _log(self, msg: str) -> None:
        self.log.append(f"{datetime.now(UTC):%H:%M:%S} {msg}")
        self.log = self.log[-200:]


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=12)


def create_app(settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    settings = settings or get_settings()
    if conn is None:
        from ..state import restore_if_missing

        restore_if_missing(settings)
        conn = connect(settings.kt_db_path)
    app = FastAPI(title="Killing Time - Raid Progress", version=__version__)
    app.state.settings = settings
    app.state.conn = conn
    app.state.sync_manager = SyncManager(settings, settings.kt_db_path)
    app.state.asker = None
    if settings.kt_auto_sync and conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"] == 0:
        threading.Timer(3.0, app.state.sync_manager.start).start()
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["date"] = lambda ms: metrics.ms_to_date(ms) or "-"
    templates.env.filters["num"] = lambda v, d=0: ("-" if v is None else (f"{v:,.{d}f}"))
    templates.env.filters["pct0"] = lambda v: ("—" if v is None else f"{round(v)}")
    templates.env.globals["DIFFICULTIES"] = DIFFICULTIES
    templates.env.globals["version"] = __version__
    db_lock = threading.Lock()

    def link(path: str, **params: Any) -> str:
        q = {k: v for k, v in params.items() if v not in (None, "", False)}
        return f"{path}?{urlencode(q)}" if q else path

    templates.env.globals["link"] = link

    # ------------------------------------------------------------------ teams
    def known_teams() -> list[str]:
        seen = metrics.teams_seen(conn)
        return [t for t in settings.team_names if t in seen] or seen

    def team_from_slug(slug: str) -> str | None:
        """Team name for a URL slug; ``guild`` -> None (whole guild). Unknown slugs raise 404."""
        if slug == GUILD:
            return None
        for t in known_teams():
            if slugify(t) == slug:
                return t
        raise HTTPException(status_code=404, detail="Unknown team")

    def team_slug(team: str | None) -> str:
        return GUILD if team is None else slugify(team)

    def remembered_slug(request: Request) -> str | None:
        slug = request.cookies.get(TEAM_COOKIE)
        if not slug:
            return None
        if slug == GUILD or any(slugify(t) == slug for t in known_teams()):
            return slug
        return None

    def with_cookie(response, slug: str):
        response.set_cookie(TEAM_COOKIE, slug, max_age=365 * 86400, samesite="lax")
        return response

    # ------------------------------------------------------------------ context
    def pick_zone(tiers: list[dict], zone: int | None) -> int | None:
        if zone and any(t["id"] == zone for t in tiers):
            return zone
        return tiers[0]["id"] if tiers else None

    def pick_diff(raw: int | None, zone_id: int | None, team: str | None) -> int:
        if raw in (3, 4, 5):
            return raw
        return metrics.best_difficulty(conn, zone_id, team) if zone_id else 5

    def ctx(request: Request, slug: str | None = None, *, tier: int | None = None, d: int | None = None, **extra: Any) -> dict[str, Any]:
        team = team_from_slug(slug) if slug else None
        with db_lock:
            ov = metrics.overview(conn, team)
            teams = known_teams()
            zone_id = pick_zone(ov["tiers"], tier) if slug else None
            diff = pick_diff(d, zone_id, team) if slug else None
            unattributed = metrics.unattributed_reports(conn, zone_id) if (zone_id and team) else 0
        tslug = team_slug(team) if slug else None
        return {
            "request": request,
            "settings": settings,
            "overview": ov,
            "team": team,
            "team_slug": tslug,
            "team_label": team or ("Whole guild" if slug else None),
            "teams": [{"name": t, "slug": slugify(t)} for t in teams],
            "base": f"/t/{tslug}" if tslug else "",
            "zone_id": zone_id,
            "zone": next((t for t in ov["tiers"] if t["id"] == zone_id), None),
            "difficulty": diff,
            "unattributed": unattributed,
            "ask_enabled": settings.ask_configured,
            "sync_running": app.state.sync_manager.running,
            **extra,
        }

    # ------------------------------------------------------------------ home / chooser
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with db_lock:
            has_data = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"] > 0
        if not has_data:
            return templates.TemplateResponse(request, "empty.html", ctx(request))
        slug = remembered_slug(request)
        if slug:
            return RedirectResponse(f"/t/{slug}/", status_code=302)
        return teams_page(request)

    @app.get("/teams", response_class=HTMLResponse)
    def teams_page(request: Request):
        with db_lock:
            cards = metrics.team_cards(conn, known_teams())
        for c in cards:
            c["slug"] = team_slug(c["team"])
        return templates.TemplateResponse(request, "teams.html", ctx(request, cards=cards))

    # ------------------------------------------------------------------ team pages
    def peer_opts(peers: str | None, above: int | None, below: int | None) -> dict[str, Any]:
        mode = peers if peers in metrics.PEER_MODES else "level"
        return {"mode": mode, "above": 20 if above is None else max(0, min(above, 200)), "below": 20 if below is None else max(0, min(below, 200))}

    @app.get("/t/{slug}/", response_class=HTMLResponse)
    def progress_page(request: Request, slug: str, tier: int | None = None, d: int | None = None,
                      peers: str | None = None, above: int | None = None, below: int | None = None):
        c = ctx(request, slug, tier=tier, d=d)
        team, zone_id, diff = c["team"], c["zone_id"], c["difficulty"]
        if not zone_id:
            return with_cookie(templates.TemplateResponse(request, "progress.html", {**c, "summary": None}), slug)
        with db_lock:
            summary = metrics.tier_summary(conn, zone_id, diff, team)
            timeline = metrics.progress_timeline(conn, zone_id, diff, team)
            nights = metrics.raid_nights(conn, zone_id, limit=10, team=team)
            other = {dd: metrics.tier_summary(conn, zone_id, dd, team) for dd in (5, 4, 3)}
            latest = metrics.latest_kills(conn, limit=6, team=team, zone_id=zone_id)
            perf = metrics.performance(conn, zone_id, diff, team)
            rio = next((r for r in c["overview"]["raiderio"] if r["raid_slug"] == c["zone"]["rio_raid_slug"]), None)
            wcl_rank = conn.execute(
                "SELECT * FROM wcl_zone_rankings WHERE zone_id = ? AND metric = 'progress'", (zone_id,)).fetchone()
            popts = peer_opts(peers, above, below)
            prev_slug = None
            if popts["mode"] == "cohort":
                raids = [r for r in metrics.rio_raids_for_zone(conn) if r["slug"] != c["zone"]["rio_raid_slug"] and r["bosses"] > 1]
                prev_slug = raids[0]["slug"] if raids else None
            peers = metrics.peer_comparison(conn, c["zone"]["rio_raid_slug"], diff, team, prev_raid_slug=prev_slug, **popts) if c["zone"]["rio_raid_slug"] else None
        peer_by_slug = {b["slug"]: b for b in peers["bosses"]} if peers else {}
        for b in summary["bosses"]:
            b["peer"] = peer_by_slug.get(b["rio_encounter_slug"])
        difficulties = [
            {"code": dd, "name": DIFFICULTIES[dd], "killed": s["killed"], "total": s["total_bosses"], "pulls": s["pulls"],
             "cleared": s["cleared"], "active": dd == diff}
            for dd, s in other.items() if s["pulls"] > 0 or dd == diff
        ]
        resp = templates.TemplateResponse(
            request, "progress.html",
            {**c, "summary": summary, "timeline": timeline, "nights": nights, "difficulties": difficulties, "latest": latest,
             "perf": perf, "rio": rio, "wcl_rank": dict(wcl_rank) if wcl_rank else None, "peers": peers,
             "chart_data": json.dumps({"summary": summary, "timeline": timeline, "nights": nights,
                                       "peers": {"peer_count": peers["peer_count"]} if peers else None}, default=str)},
        )
        return with_cookie(resp, slug)

    @app.get("/t/{slug}/roster", response_class=HTMLResponse)
    def roster_page(request: Request, slug: str, tier: int | None = None, d: int | None = None, pugs: int = 0):
        c = ctx(request, slug, tier=tier, d=d)
        with db_lock:
            data = metrics.roster(conn, c["zone_id"], c["difficulty"], c["team"], include_pugs=bool(pugs)) if c["zone_id"] else None
            coverage = metrics.parse_coverage(conn, c["zone_id"]) if c["zone_id"] else None
        return with_cookie(templates.TemplateResponse(
            request, "roster.html",
            {**c, "data": data, "coverage": coverage, "pugs": bool(pugs), "chart_data": json.dumps(data, default=str)}), slug)

    @app.get("/t/{slug}/meet", response_class=HTMLResponse)
    def meet_page(request: Request, slug: str, tier: int | None = None, d: int | None = None):
        c = ctx(request, slug, tier=tier, d=d)
        with db_lock:
            cards = metrics.meet_the_team(conn, c["zone_id"], c["difficulty"], c["team"], alts=settings.alts,
                                          image_url=settings.member_image_url, exclude=settings.excluded) if c["zone_id"] else []
        return with_cookie(templates.TemplateResponse(request, "meet.html", {**c, "cards": cards}), slug)

    @app.get("/t/{slug}/boss/{encounter_id}", response_class=HTMLResponse)
    def boss_page(request: Request, slug: str, encounter_id: int, tier: int | None = None, d: int | None = None):
        c = ctx(request, slug, tier=tier, d=d)
        with db_lock:
            data = metrics.boss_pulls(conn, c["zone_id"], encounter_id, c["difficulty"], c["team"]) if c["zone_id"] else None
            other = {dd: metrics.boss_pulls(conn, c["zone_id"], encounter_id, dd, c["team"])
                     for dd in (5, 4, 3)} if c["zone_id"] else {}
        return with_cookie(templates.TemplateResponse(
            request, "boss.html",
            {**c, "data": data, "other": other, "chart_data": json.dumps(data, default=str)}), slug)

    @app.get("/api/boss/{zone_id}/{encounter_id}")
    def api_boss(zone_id: int, encounter_id: int, difficulty: int = 5, team: str | None = None):
        with db_lock:
            return as_json(metrics.boss_pulls(conn, zone_id, encounter_id, difficulty, team))

    @app.post("/api/stories")
    def api_stories(refresh: int = 0, limit: int = 200):
        """Write the Meet the Team stories. Slow and costs Anthropic tokens, so it is never part of a sync."""
        from ..stories import write_stories

        with db_lock:
            cur = metrics.current_tier(conn)
            if not cur:
                return {"written": 0, "kept": 0, "failed": 0, "note": "no tier to write about yet"}
            cards = metrics.meet_the_team(conn, cur["id"], metrics.best_difficulty(conn, cur["id"]), min_raids=2,
                                          alts=settings.alts, image_url=settings.member_image_url,
                                          exclude=settings.excluded)
            tally = write_stories(conn, cards, settings, refresh=bool(refresh), limit=limit)
        from ..state import configured, persist

        if configured(settings):
            persist(settings)   # the stories are worth keeping across a container restart
        return tally

    @app.get("/api/alts")
    def api_alts(min_nights: int = 3, everyone: int = 0, min_score: float = 0.45):
        """Characters that look like alts of someone on the roster. Suggestions only - confirm them in RAID_ALTS.

        Defaults to people who raid the current tier, since a list of every pairing across eight years of logs is
        noise; pass everyone=1 for the lot."""
        with db_lock:
            only = None
            cur = None if everyone else metrics.current_tier(conn)
            if cur:
                only = {r["player_name"] for r in metrics._rows(
                    conn, "SELECT DISTINCT player_name FROM v_attendance WHERE zone_id = ? AND presence = 1",
                    (cur["id"],))}
            return as_json(metrics.alt_candidates(conn, min_nights=min_nights, only=only, min_score=min_score))

    @app.get("/api/meet/{zone_id}")
    def api_meet(zone_id: int, difficulty: int | None = None, team: str | None = None):
        with db_lock:
            return as_json(metrics.meet_the_team(conn, zone_id, difficulty, team, alts=settings.alts,
                                                 image_url=settings.member_image_url, exclude=settings.excluded))

    @app.get("/t/{slug}/nights", response_class=HTMLResponse)
    def nights_page(request: Request, slug: str, tier: int | None = None, d: int | None = None):
        c = ctx(request, slug, tier=tier, d=d)
        with db_lock:
            nights = metrics.raid_nights(conn, c["zone_id"], limit=200, team=c["team"]) if c["zone_id"] else []
        return with_cookie(templates.TemplateResponse(
            request, "nights.html", {**c, "nights": nights, "chart_data": json.dumps(nights, default=str)}), slug)

    @app.get("/t/{slug}/history", response_class=HTMLResponse)
    def history_page(request: Request, slug: str, d: int | None = None):
        c = ctx(request, slug, d=d)
        team = c["team"]
        diff = d if d in (3, 4, 5) else 5
        with db_lock:
            comparison = metrics.tier_comparison(conn, diff, team)
            if not comparison and diff == 5 and d is None:
                diff = 4
                comparison = metrics.tier_comparison(conn, diff, team)
            tiers_all = [{**t, "by_diff": {dd: metrics.tier_summary(conn, t["id"], dd, team) for dd in (5, 4, 3) if dd in t["kills"]}}
                         for t in c["overview"]["tiers"]]
        return with_cookie(templates.TemplateResponse(
            request, "history.html",
            {**c, "difficulty": diff, "comparison": comparison, "tiers_all": tiers_all, "chart_data": json.dumps(comparison, default=str)}), slug)

    def raid_selection(raid: str | None, d: int | None, team: str | None, zone_id: int | None) -> tuple[list[dict], dict | None, int]:
        with db_lock:
            raids = metrics.rio_raids_for_zone(conn)
        if not raids:
            return [], None, 5
        if not raid and zone_id:
            raid = next((r["slug"] for r in raids if r.get("zone_id") == zone_id), None)
        raid_slug = raid if raid and any(r["slug"] == raid for r in raids) else raids[0]["slug"]
        chosen = next(r for r in raids if r["slug"] == raid_slug)
        diff = d if d in (3, 4, 5) else None
        if diff is None:
            with db_lock:
                if chosen.get("zone_id"):
                    diff = metrics.best_difficulty(conn, chosen["zone_id"], team)
                else:
                    row = conn.execute(
                        """SELECT MAX(p.difficulty) AS d FROM rio_progress p JOIN guilds g ON g.id = p.guild_id
                           WHERE g.is_home = 1 AND p.raid_slug = ?""", (raid_slug,)).fetchone()
                    diff = int(row["d"]) if row and row["d"] else 5
        return raids, chosen, diff

    @app.get("/t/{slug}/peers", response_class=HTMLResponse)
    def peers_page(request: Request, slug: str, raid: str | None = None, tier: int | None = None, d: int | None = None,
                   peers: str | None = None, above: int | None = None, below: int | None = None):
        c = ctx(request, slug, tier=tier, d=d)
        raids, chosen, diff = raid_selection(raid, d, c["team"], c["zone_id"])
        if not chosen:
            return with_cookie(templates.TemplateResponse(request, "peers.html", {**c, "raids": [], "raid": None}), slug)
        popts = peer_opts(peers, above, below)
        with db_lock:
            others = [r for r in raids if r["slug"] != chosen["slug"] and r["bosses"] > 1]
            prev_raid = others[0] if others else None
            cmp = metrics.peer_comparison(conn, chosen["slug"], diff, c["team"], prev_raid_slug=prev_raid["slug"] if prev_raid else None, **popts)
            prev = metrics.peer_comparison(conn, prev_raid["slug"], diff, c["team"], **popts) if (prev_raid and popts["mode"] != "cohort") else None
        return with_cookie(templates.TemplateResponse(
            request, "peers.html",
            {**c, "raids": raids, "raid": chosen, "difficulty": diff, "cmp": cmp, "prev": prev, "prev_raid": prev_raid, "popts": popts,
             "modes": metrics.PEER_MODES,
             "chart_data": json.dumps({"cmp": cmp, "prev": prev, "prev_raid": prev_raid}, default=str)}), slug)

    @app.get("/t/{slug}/realm", response_class=HTMLResponse)
    def realm_page(request: Request, slug: str, raid: str | None = None, tier: int | None = None, d: int | None = None):
        c = ctx(request, slug, tier=tier, d=d)
        raids, chosen, diff = raid_selection(raid, d, None, c["zone_id"])
        if not chosen:
            return with_cookie(templates.TemplateResponse(request, "rivals.html", {**c, "raids": [], "raid": None}), slug)
        with db_lock:
            comparison = metrics.rival_comparison(conn, chosen["slug"], diff)
            standings = metrics.realm_standings(conn, chosen["slug"], diff, limit=100)
            race = metrics.race_timeline(conn, chosen["slug"], diff)
        return with_cookie(templates.TemplateResponse(
            request, "rivals.html",
            {**c, "raids": raids, "raid": chosen, "difficulty": diff, "comparison": comparison, "standings": standings,
             "chart_data": json.dumps({"comparison": comparison, "race": race}, default=str)}), slug)

    # Old flat URLs keep working: send them to the remembered team (or the whole guild).
    LEGACY = {"/tiers": "history", "/peers": "peers", "/rivals": "realm", "/performance": "roster", "/attendance": "roster", "/nights": "nights"}
    for old, new in LEGACY.items():
        def _redirect(request: Request, _new: str = new):
            slug = remembered_slug(request) or GUILD
            q = {k: v for k, v in request.query_params.items() if k not in ("team", "zone", "difficulty")}
            if request.query_params.get("team") in known_teams():
                slug = slugify(request.query_params["team"])
            if request.query_params.get("zone"):
                q["tier"] = request.query_params["zone"]
            if request.query_params.get("difficulty"):
                q["d"] = request.query_params["difficulty"]
            return RedirectResponse(link(f"/t/{slug}/{_new}", **q), status_code=302)
        app.get(old, include_in_schema=False)(_redirect)

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request):
        return templates.TemplateResponse(request, "ask.html", ctx(request))

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request):
        c = ctx(request)
        with db_lock:
            logs = [dict(r) for r in conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 15").fetchall()]
            for entry in logs:
                try:
                    entry["detail"] = json.loads(entry["detail"]) if entry["detail"] else {}
                except ValueError:
                    entry["detail"] = {}
            zones = [dict(r) for r in conn.execute(
                """SELECT z.id, z.name, z.rio_raid_slug, x.name AS expansion,
                          (SELECT COUNT(*) FROM reports r WHERE r.zone_id = z.id) AS reports,
                          (SELECT COUNT(*) FROM reports r WHERE r.zone_id = z.id AND r.rankings_synced_at IS NOT NULL) AS parsed_reports,
                          (SELECT COUNT(*) FROM reports r JOIN report_teams t ON t.report_code = r.code WHERE r.zone_id = z.id) AS attributed,
                          (SELECT COUNT(*) FROM encounters e WHERE e.zone_id = z.id) AS bosses,
                          (SELECT COUNT(*) FROM encounters e WHERE e.zone_id = z.id AND e.rio_encounter_slug IS NOT NULL) AS mapped_bosses
                   FROM zones z LEFT JOIN expansions x ON x.id = z.expansion_id ORDER BY z.id DESC""").fetchall()]
            rl = conn.execute("SELECT value FROM meta WHERE key = 'wcl_rate_limit'").fetchone()
            counts = {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                      for t in ("reports", "fights", "attendance", "parses", "guilds", "rio_progress")}
            team_counts = [dict(r) for r in conn.execute(
                "SELECT team, COUNT(*) AS reports FROM report_teams GROUP BY team ORDER BY reports DESC").fetchall()]
            unassigned = conn.execute(
                "SELECT COUNT(*) AS c FROM reports r WHERE NOT EXISTS (SELECT 1 FROM report_teams t WHERE t.report_code = r.code)").fetchone()["c"]
        return templates.TemplateResponse(
            request, "status.html",
            {**c, "logs": logs, "zones": zones, "rate_limit": json.loads(rl["value"]) if rl else None,
             "counts": counts, "sync": app.state.sync_manager, "team_counts": team_counts, "unassigned": unassigned},
        )

    @app.get("/public", response_class=HTMLResponse)
    def public_page():
        from ..public import render_public_page

        with db_lock:
            return HTMLResponse(render_public_page(conn, settings))

    @app.get("/public/join", response_class=HTMLResponse)
    def public_join_page():
        """The recruitment page as the public site shows it (killingtime.fyi/join)."""
        from ..public import render_public_join_page

        with db_lock:
            return HTMLResponse(render_public_join_page(conn, settings))

    @app.get("/public/team", response_class=HTMLResponse)
    def public_team_page():
        """Meet the Team as the public site shows it (killingtime.fyi/team)."""
        from ..public import render_public_team_page

        with db_lock:
            return HTMLResponse(render_public_team_page(conn, settings))

    # ------------------------------------------------------------------ api
    @app.post("/api/sync")
    def api_sync(full: bool = False):
        started = app.state.sync_manager.start(full=full)
        return {"started": started, "running": app.state.sync_manager.running}

    @app.get("/api/sync/status")
    def api_sync_status():
        m = app.state.sync_manager
        return {"running": m.running, "log": m.log[-40:], "last_result": m.last_result}

    @app.post("/sync")
    def sync_form(full: bool = False):
        app.state.sync_manager.start(full=full)
        return RedirectResponse("/status", status_code=303)

    def as_json(data: Any) -> JSONResponse:
        return JSONResponse(json.loads(json.dumps(data, default=str)))

    @app.get("/api/overview")
    def api_overview(team: str | None = None):
        with db_lock:
            return as_json(metrics.overview(conn, team))

    @app.get("/api/public")
    def api_public():
        from ..public import public_summary

        with db_lock:
            return as_json(public_summary(conn, settings))

    @app.get("/api/tier/{zone_id}")
    def api_tier(zone_id: int, difficulty: int = 5, team: str | None = None):
        with db_lock:
            return as_json(metrics.tier_summary(conn, zone_id, difficulty, team))

    @app.get("/api/rivals/{raid_slug}")
    def api_rivals(raid_slug: str, difficulty: int = 5):
        with db_lock:
            return as_json(metrics.rival_comparison(conn, raid_slug, difficulty))

    @app.get("/api/peers/{raid_slug}")
    def api_peers(raid_slug: str, difficulty: int = 5, team: str | None = None, peers: str | None = None,
                  above: int | None = None, below: int | None = None, prev: str | None = None):
        with db_lock:
            return as_json(metrics.peer_comparison(conn, raid_slug, difficulty, team, prev_raid_slug=prev, **peer_opts(peers, above, below)))

    @app.get("/api/performance/{zone_id}")
    def api_performance(zone_id: int, difficulty: int | None = None, team: str | None = None, pugs: bool = False):
        with db_lock:
            return as_json(metrics.performance(conn, zone_id, difficulty, team, include_pugs=pugs))

    @app.get("/api/roster/{zone_id}")
    def api_roster(zone_id: int, difficulty: int | None = None, team: str | None = None, pugs: bool = False):
        with db_lock:
            return as_json(metrics.roster(conn, zone_id, difficulty, team, include_pugs=pugs))

    @app.post("/api/ask")
    def api_ask(body: AskRequest):
        if not settings.ask_configured:
            raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured")
        from ..ask import Asker, describe_error

        if app.state.asker is None:
            app.state.asker = Asker(conn, settings)
        try:
            result = app.state.asker.ask(body.question, body.history)
        except Exception as exc:  # noqa: BLE001
            log.exception("ask failed")
            raise HTTPException(status_code=502, detail=describe_error(exc)) from exc
        return {
            "answer": result.answer,
            "charts": result.charts,
            "trace": result.trace,
            "usage": result.usage,
            "model": result.model,
            "elapsed_s": result.elapsed_s,
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#1a1a19"/>'
               '<text x="16" y="23" font-size="20" text-anchor="middle" fill="#3987e5" font-family="sans-serif">⚔</text></svg>')
        return HTMLResponse(svg, media_type="image/svg+xml")

    return app


app = None  # created lazily by `kt serve`; `uvicorn killingtime.web.app:create_app --factory` also works
