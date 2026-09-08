"""``kt`` command line: check, sync, serve, ask, report."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

from . import __version__, metrics
from .config import Settings, get_settings
from .db import DIFFICULTIES, connect


def _wcl(settings: Settings):
    from .wcl import WCLClient

    if not settings.wcl_configured:
        return None
    return WCLClient(settings.wcl_client_id, settings.wcl_client_secret, settings.wcl_token_url, settings.wcl_api_url, settings.wcl_token_cache)


def _rio(settings: Settings):
    from .raiderio import RaiderIOClient

    return RaiderIOClient(settings.rio_api_url)


def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    ok = True
    print(f"killingtime {__version__}")
    print(f"guild:        {settings.guild_name} @ {settings.guild_realm} ({settings.guild_region.upper()})")
    print(f"database:     {settings.kt_db_path}")
    print(f"rivals:       {', '.join(r.name for r in settings.rivals) or '(none configured)'}")
    if settings.wcl_configured:
        try:
            wcl = _wcl(settings)
            rl = wcl.rate_limit()
            g = wcl.guild(settings.home_guild.name, settings.home_guild.realm_slug, settings.guild_region.upper())
            found = f"found (id {g['id']})" if g else "NOT FOUND - check GUILD_* settings"
            print(f"warcraftlogs: OK - points used {rl['pointsSpentThisHour']:.0f}/{rl['limitPerHour']} this hour; guild {found}")
            ok &= bool(g)
        except Exception as exc:  # noqa: BLE001
            print(f"warcraftlogs: FAILED - {exc}")
            ok = False
    else:
        print("warcraftlogs: not configured (WCL_CLIENT_ID / WCL_CLIENT_SECRET empty) - pull-level data will be missing")
    try:
        prof = _rio(settings).guild_profile(settings.home_guild.region, settings.home_guild.realm_slug, settings.guild_name)
        raids = ", ".join(f"{k}: {v['summary']}" for k, v in (prof.get("raid_progression") or {}).items())
        print(f"raider.io:    OK - {prof.get('faction')} - {raids}")
    except Exception as exc:  # noqa: BLE001
        print(f"raider.io:    FAILED - {exc}")
        ok = False
    if settings.ask_configured:
        print(f"ask:          configured (model {settings.ask_model}, effort {settings.ask_effort})")
    else:
        print("ask:          ANTHROPIC_API_KEY not set - the Ask page will be disabled")
    return 0 if ok else 1


def cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    from .sync import run_sync

    conn = connect(settings.kt_db_path)
    wcl = None if args.no_wcl else _wcl(settings)
    rio = None if args.no_rio else _rio(settings)
    t0 = time.time()
    stats = run_sync(conn, settings, wcl, rio, full=args.full, skip_attendance=args.skip_attendance, progress=lambda m: print(m, flush=True))
    print(
        f"\nDone in {time.time() - t0:.0f}s: {stats.reports} reports, {stats.fights} pulls, "
        f"{stats.attendance_rows} attendance rows, {stats.rio_guilds} guilds tracked, "
        f"{stats.rio_progress_rows} raider.io progress rows; {stats.wcl_queries} WCL queries, {stats.rio_requests} raider.io requests."
    )
    if stats.warnings:
        print(f"{len(stats.warnings)} warning(s):")
        for w in stats.warnings[:20]:
            print("  - " + w)
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from .web.app import create_app

    app = create_app(settings)
    if args.sync_every:
        def loop() -> None:
            time.sleep(5)
            while True:
                try:
                    app.state.sync_manager.start(full=False)
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception("scheduled sync failed")
                time.sleep(args.sync_every * 60)

        threading.Thread(target=loop, daemon=True, name="kt-sync-scheduler").start()
        print(f"background sync every {args.sync_every} minutes")
    uvicorn.run(app, host=args.host or settings.kt_host, port=args.port or settings.kt_port, log_level="info")
    return 0


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    from .ask import Asker, describe_error

    if not settings.ask_configured:
        print("ANTHROPIC_API_KEY is not set. Add it to .env (see docs/SETUP.md step 5).", file=sys.stderr)
        return 2
    conn = connect(settings.kt_db_path)
    question = " ".join(args.question).strip()
    try:
        result = Asker(conn, settings).ask(question)
    except Exception as exc:  # noqa: BLE001
        print(describe_error(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.__dict__, indent=2, default=str))
        return 0
    print(result.answer)
    for i, ch in enumerate(result.charts, start=1):
        print(f"\n[chart {i}] {ch['title']} ({ch['type']})")
        width = max(len(c) for c in ch["categories"]) if ch["categories"] else 10
        print(" " * width + "  " + " | ".join(s["name"] for s in ch["series"]))
        for idx, cat in enumerate(ch["categories"]):
            vals = " | ".join("" if s["data"][idx] is None else f"{s['data'][idx]:g}" for s in ch["series"])
            print(f"{cat:<{width}}  {vals}")
    if args.verbose:
        print(f"\n-- {result.model}, {result.elapsed_s}s, tokens in/out {result.usage['input_tokens']}/{result.usage['output_tokens']} "
              f"(cache read {result.usage['cache_read_input_tokens']}), {len(result.trace)} tool calls")
        for t in result.trace:
            print(f"   {t['tool']}: {json.dumps(t['input'])[:200]}")
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.kt_db_path)
    ov = metrics.overview(conn)
    g = ov["guild"] or {}
    print(f"{g.get('name', settings.guild_name)} - {g.get('realm_slug', '')} {g.get('region', '').upper()}  (last sync {ov['last_sync'] or 'never'})")
    if ov["raiderio"]:
        print("\nRaider.IO:")
        for r in ov["raiderio"]:
            ranks = []
            if r["mythic_world"]:
                ranks.append(f"M world #{r['mythic_world']} realm #{r['mythic_realm']}")
            if r["heroic_world"]:
                ranks.append(f"H world #{r['heroic_world']} realm #{r['heroic_realm']}")
            print(f"  {r['name'] or r['raid_slug']:<32} {r['summary']:<8} {'; '.join(ranks)}")
    tiers = ov["tiers"]
    if not tiers:
        print("\nNo Warcraft Logs data yet - run `kt sync`.")
        return 0
    zone_id = args.zone or tiers[0]["id"]
    diff = args.difficulty or metrics.best_difficulty(conn, zone_id)
    s = metrics.tier_summary(conn, zone_id, diff)
    print(f"\n{s['zone']['name']} - {s['difficulty_name']}: {s['killed']}/{s['total_bosses']} bosses, "
          f"{s['pulls']} pulls over {s['nights']} nights ({s['hours']}h in combat), first pull {s['first_pull_date']}")
    print(f"  {'Boss':<34} {'Kill date':<12} {'Pulls':>6} {'Nights':>7} {'Best %':>7}")
    for b in s["bosses"]:
        best = "" if b["best_pct"] is None else f"{b['best_pct']:.1f}"
        print(f"  {b['name']:<34} {b['first_kill_date'] or '-':<12} {b['total_pulls'] or 0:>6} {b['nights_to_kill'] or 0:>7} {best:>7}")
    print("\nTiers with data:")
    for t in tiers:
        print(f"  [{t['id']}] {t['name']:<36} {t['summary']}  ({t['first_report_date']} -> {t['last_report_date']})")
    print(f"\nDifficulties: {DIFFICULTIES}")
    return 0


def cmd_stories(args: argparse.Namespace, settings: Settings) -> int:
    """Write the Meet the Team stories. Costs Anthropic tokens, so it is a command rather than part of a sync."""
    from .stories import write_stories

    conn = connect(settings.kt_db_path)
    cur = metrics.current_tier(conn)
    if not cur:
        print("no tier with our logs in it yet")
        return 1
    cards = metrics.meet_the_team(conn, cur["id"], metrics.best_difficulty(conn, cur["id"]), min_raids=2,
                                  alts=settings.alts, image_url=settings.member_image_url,
                                  exclude=settings.excluded)
    tally = write_stories(conn, cards, settings, refresh=args.refresh, limit=args.limit, progress=print)
    if args.show:
        for row in conn.execute("SELECT player_name, shape, story FROM bios ORDER BY player_name"):
            print(f"\n--- {row['player_name']} ({row['shape']}) ---\n{row['story']}")
    return 0 if not tally["failed"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kt", description="Killing Time raid progress tracker")
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="validate configuration and API access").set_defaults(fn=cmd_check)

    s = sub.add_parser("sync", help="pull data from Warcraft Logs and Raider.IO")
    s.add_argument("--full", action="store_true", help="re-list all reports instead of only recent ones")
    s.add_argument("--skip-attendance", action="store_true")
    s.add_argument("--no-wcl", action="store_true", help="skip Warcraft Logs")
    s.add_argument("--no-rio", action="store_true", help="skip Raider.IO")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("serve", help="run the web dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--sync-every", type=int, metavar="MINUTES", help="run an incremental sync on a schedule")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("ask", help="ask a question about the guild's progress")
    s.add_argument("question", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("stories", help="write the Meet the Team bios with Claude")
    s.add_argument("--refresh", action="store_true", help="rewrite every story, not only the changed ones")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--show", action="store_true", help="print the stories afterwards")
    s.set_defaults(fn=cmd_stories)

    s = sub.add_parser("report", help="print a text progress report")
    s.add_argument("--zone", type=int, help="Warcraft Logs zone id (default: current tier)")
    s.add_argument("--difficulty", type=int, choices=[3, 4, 5])
    s.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    return int(args.fn(args, settings) or 0)


if __name__ == "__main__":
    sys.exit(main())
