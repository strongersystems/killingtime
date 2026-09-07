"""FastAPI web app: dashboard, tier comparison, rivals, attendance, raid nights and Ask."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
            self.last_result = f"ok: {stats.reports} reports, {stats.fights} pulls, {stats.rio_progress_rows} raider.io rows, {len(stats.warnings)} warnings"
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
    templates.env.globals["DIFFICULTIES"] = DIFFICULTIES
    templates.env.globals["version"] = __version__
    db_lock = threading.Lock()

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        with db_lock:
            ov = metrics.overview(conn)
        return {
            "request": request,
            "settings": settings,
            "overview": ov,
            "ask_enabled": settings.ask_configured,
            "sync_running": app.state.sync_manager.running,
            **extra,
        }

    def parse_diff(raw: str | None, zone_id: int | None) -> int:
        if raw and raw.isdigit() and int(raw) in DIFFICULTIES:
            return int(raw)
        return metrics.best_difficulty(conn, zone_id) if zone_id else 5

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, zone: int | None = None, difficulty: str | None = None):
        c = ctx(request)
        tiers = c["overview"]["tiers"]
        if not tiers:
            return templates.TemplateResponse(request, "empty.html", c)
        zone_id = zone if zone and any(t["id"] == zone for t in tiers) else tiers[0]["id"]
        diff = parse_diff(difficulty, zone_id)
        with db_lock:
            summary = metrics.tier_summary(conn, zone_id, diff)
            timeline = metrics.progress_timeline(conn, zone_id, diff)
            nights = metrics.raid_nights(conn, zone_id, limit=12)
            other_diffs = {
                d: metrics.tier_summary(conn, zone_id, d)["killed"] for d in (3, 4, 5) if d != diff
            }
            rankings = [dict(r) for r in conn.execute(
                "SELECT * FROM wcl_zone_rankings WHERE zone_id = ? ORDER BY metric", (zone_id,)).fetchall()]
        return templates.TemplateResponse(
            request, "dashboard.html",
            {**c, "zone_id": zone_id, "difficulty": diff, "summary": summary, "timeline": timeline,
             "nights": nights, "other_diffs": other_diffs, "rankings": rankings,
             "chart_data": json.dumps({"summary": summary, "timeline": timeline, "nights": nights}, default=str)},
        )

    @app.get("/tiers", response_class=HTMLResponse)
    def tiers_page(request: Request, difficulty: str | None = None):
        c = ctx(request)
        diff = int(difficulty) if difficulty and difficulty.isdigit() and int(difficulty) in DIFFICULTIES else 5
        with db_lock:
            comparison = metrics.tier_comparison(conn, diff)
            if not comparison and diff == 5:
                diff = 4
                comparison = metrics.tier_comparison(conn, diff)
        return templates.TemplateResponse(
            request, "tiers.html",
            {**c, "difficulty": diff, "comparison": comparison, "chart_data": json.dumps(comparison, default=str)},
        )

    @app.get("/rivals", response_class=HTMLResponse)
    def rivals_page(request: Request, raid: str | None = None, difficulty: str | None = None):
        c = ctx(request)
        with db_lock:
            raids = metrics.rio_raids_for_zone(conn)
        if not raids:
            return templates.TemplateResponse(request, "rivals.html", {**c, "raids": [], "raid": None})
        raid_slug = raid if raid and any(r["slug"] == raid for r in raids) else raids[0]["slug"]
        diff = int(difficulty) if difficulty and difficulty.isdigit() and int(difficulty) in (3, 4, 5) else None
        with db_lock:
            if diff is None:
                # default to the highest difficulty where the home guild has any Raider.IO progress
                row = conn.execute(
                    """SELECT MAX(p.difficulty) AS d FROM rio_progress p JOIN guilds g ON g.id = p.guild_id
                       WHERE g.is_home = 1 AND p.raid_slug = ?""", (raid_slug,)).fetchone()
                diff = int(row["d"]) if row and row["d"] else 5
            comparison = metrics.rival_comparison(conn, raid_slug, diff)
            standings = metrics.realm_standings(conn, raid_slug, diff, limit=100)
            race = metrics.race_timeline(conn, raid_slug, diff)
        return templates.TemplateResponse(
            request, "rivals.html",
            {**c, "raids": raids, "raid": next(r for r in raids if r["slug"] == raid_slug), "difficulty": diff,
             "comparison": comparison, "standings": standings,
             "chart_data": json.dumps({"comparison": comparison, "race": race}, default=str)},
        )

    @app.get("/attendance", response_class=HTMLResponse)
    def attendance_page(request: Request, zone: int | None = None):
        c = ctx(request)
        tiers = c["overview"]["tiers"]
        zone_id = zone if zone and any(t["id"] == zone for t in tiers) else (tiers[0]["id"] if tiers else None)
        with db_lock:
            data = metrics.attendance_summary(conn, zone_id) if zone_id else {"total_raids": 0, "players": []}
        return templates.TemplateResponse(
            request, "attendance.html",
            {**c, "zone_id": zone_id, "data": data, "chart_data": json.dumps(data, default=str)},
        )

    @app.get("/nights", response_class=HTMLResponse)
    def nights_page(request: Request, zone: int | None = None):
        c = ctx(request)
        tiers = c["overview"]["tiers"]
        zone_id = zone if zone and any(t["id"] == zone for t in tiers) else (tiers[0]["id"] if tiers else None)
        with db_lock:
            nights = metrics.raid_nights(conn, zone_id, limit=200) if zone_id else []
        return templates.TemplateResponse(
            request, "nights.html",
            {**c, "zone_id": zone_id, "nights": nights, "chart_data": json.dumps(nights, default=str)},
        )

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
                          (SELECT COUNT(*) FROM encounters e WHERE e.zone_id = z.id) AS bosses,
                          (SELECT COUNT(*) FROM encounters e WHERE e.zone_id = z.id AND e.rio_encounter_slug IS NOT NULL) AS mapped_bosses
                   FROM zones z LEFT JOIN expansions x ON x.id = z.expansion_id ORDER BY z.id DESC""").fetchall()]
            rl = conn.execute("SELECT value FROM meta WHERE key = 'wcl_rate_limit'").fetchone()
            counts = {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                      for t in ("reports", "fights", "attendance", "guilds", "rio_progress")}
        return templates.TemplateResponse(
            request, "status.html",
            {**c, "logs": logs, "zones": zones, "rate_limit": json.loads(rl["value"]) if rl else None,
             "counts": counts, "sync": app.state.sync_manager},
        )

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

    @app.get("/api/overview")
    def api_overview():
        with db_lock:
            return JSONResponse(json.loads(json.dumps(metrics.overview(conn), default=str)))

    @app.get("/api/tier/{zone_id}")
    def api_tier(zone_id: int, difficulty: int = 5):
        with db_lock:
            return JSONResponse(json.loads(json.dumps(metrics.tier_summary(conn, zone_id, difficulty), default=str)))

    @app.get("/api/rivals/{raid_slug}")
    def api_rivals(raid_slug: str, difficulty: int = 5):
        with db_lock:
            return JSONResponse(json.loads(json.dumps(metrics.rival_comparison(conn, raid_slug, difficulty), default=str)))

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
