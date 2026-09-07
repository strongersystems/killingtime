"""Raid teams, peer comparison, parses and the public site."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from killingtime import metrics, state
from killingtime.config import Settings, parse_raid_teams
from killingtime.db import connect
from killingtime.public import public_summary, render_public_page
from killingtime.sync import drop_zone, is_raid_zone, match_name, run_sync
from killingtime.web.app import create_app


def test_parse_raid_teams():
    teams = parse_raid_teams("CE Team: Nórmán, Elelena ; 6 Hour Team: Andrewro,Billadin;")
    assert teams == {"CE Team": ["Nórmán", "Elelena"], "6 Hour Team": ["Andrewro", "Billadin"]}
    assert parse_raid_teams("") == {}


def test_raid_zone_detection():
    assert is_raid_zone({"difficulties": [{"id": 5}, {"id": 4}, {"id": 3}, {"id": 1}]})
    assert is_raid_zone({"difficulties": [{"id": 4}, {"id": 3}]})  # Blackrock Depths style event raid
    assert not is_raid_zone({"difficulties": [{"id": 10, "name": "Dungeon"}]})
    assert not is_raid_zone({"difficulties": [{"id": 108}, {"id": 109}]})  # Delves
    assert is_raid_zone({})  # no data: keep


def test_match_name_containment():
    raids = {"tier-mn-1": "MN Tier 1 (VS / DR / MQD)", "the-venomous-abyss": "The Venomous Abyss", "sporefall": "Sporefall"}
    assert match_name("VS / DR / MQD", raids, cutoff=0.85) == "tier-mn-1"
    assert match_name("The Venomous Abyss", raids, cutoff=0.85) == "the-venomous-abyss"
    assert match_name("Nerub-ar Palace", raids, cutoff=0.85) is None


def test_drop_zone_removes_everything(synced):
    conn, *_ = synced
    drop_zone(conn, 44)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM zones WHERE id = 44").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reports WHERE zone_id = 44").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fights f JOIN reports r ON r.code = f.report_code WHERE r.zone_id = 44").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM encounters WHERE zone_id = 44").fetchone()[0] == 0


def test_reports_assigned_to_teams(synced):
    conn, *_ = synced
    teams = {r["report_code"]: r["team"] for r in conn.execute("SELECT report_code, team FROM report_teams")}
    assert teams == {"C1": "6 Hour Team", "C2": "CE Team", "C3": "CE Team"}
    assert metrics.teams_seen(conn) == ["6 Hour Team", "CE Team"]
    # previous-tier reports have no attendance -> unassigned, so they only show in the guild view
    assert metrics.tiers(conn, "CE Team")[0]["id"] == 46 and len(metrics.tiers(conn, "CE Team")) == 1
    assert metrics.tiers(conn)[1]["id"] == 44


def test_team_scoped_metrics(synced):
    conn, *_ = synced
    ce = metrics.tiers(conn, "CE Team")[0]
    six = metrics.tiers(conn, "6 Hour Team")[0]
    assert ce["kills"] == {5: 1} and six["kills"] == {4: 3}
    s_ce = metrics.tier_summary(conn, 46, 5, "CE Team")
    assert s_ce["killed"] == 1 and s_ce["pulls"] == 7 and s_ce["next_boss"]["name"] == "Entombed Sentinels"
    s_six = metrics.tier_summary(conn, 46, 5, "6 Hour Team")
    assert s_six["pulls"] == 0 and s_six["killed"] == 0
    assert metrics.tier_summary(conn, 46, 4, "6 Hour Team")["cleared"] is True
    assert metrics.best_difficulty(conn, 46, "6 Hour Team") == 4 and metrics.best_difficulty(conn, 46, "CE Team") == 5
    nights = metrics.raid_nights(conn, 46, team="CE Team")
    assert [n["pull_date"] for n in nights] == ["2026-09-02", "2026-08-30"]
    att = metrics.attendance_summary(conn, 46, "6 Hour Team")
    assert att["total_raids"] == 1 and {p["player_name"] for p in att["players"]} == {"Tagrik", "Bubonic", "Sixer"}
    latest = metrics.latest_kills(conn, limit=10)
    assert latest[0]["boss"] == "Nek'zali the Soulcoiler" and latest[0]["team"] == "CE Team" and latest[0]["difficulty"] == 5
    assert {k["team"] for k in latest if k["difficulty"] == 4} == {"6 Hour Team"}
    prog = metrics.team_progress(conn, 46, ["CE Team", "6 Hour Team"])
    assert prog[0]["difficulties"][0]["killed"] == 1 and prog[1]["difficulties"][0]["name"] == "Heroic"
    cmp = metrics.tier_comparison(conn, 4, "6 Hour Team")
    assert len(cmp) == 1 and cmp[0]["killed"] == 3


def test_peer_comparison(synced):
    conn, *_ = synced
    cmp = metrics.peer_comparison(conn, "the-venomous-abyss", 5)
    assert cmp["our_kills"] == 1 and cmp["source"] == "logs"
    # only two other guilds exist, so the band widens until both qualify
    assert cmp["peer_count"] == 2 and {p["name"] for p in cmp["peers"]} == {"Internet Diff", "Advance"}
    nek = cmp["bosses"][0]
    assert nek["killed"] and nek["our_pulls"] == 3 and nek["peer_median_pulls"] == 2.5 and nek["peers_killed"] == 2
    assert nek["beat_pct"] == 0  # 2 and 3 pulls: nobody needed more than our 3
    ent = cmp["bosses"][1]
    assert not ent["killed"] and ent["our_pulls"] == 4 and ent["peer_median_pulls"] == 8.5
    assert cmp["our_total_pulls"] == 3 and cmp["peer_median_total_pulls"] == 2.5
    # team view: the 6 Hour Team has no mythic pulls, so nothing of its own to compare
    team_cmp = metrics.peer_comparison(conn, "the-venomous-abyss", 5, team="6 Hour Team")
    assert team_cmp["our_kills"] == 0
    # heroic: our logs say 3/3, peers come from the heroic leaderboard
    h = metrics.peer_comparison(conn, "the-venomous-abyss", 4, team="6 Hour Team")
    assert h["our_kills"] == 3 and h["bosses"][0]["our_pulls"] == 1


def test_parses_synced_and_summarised(synced):
    conn, wcl, rio, settings = synced
    # every kill fight in the current + previous tier got tank/dps rows from the dps query and healer rows from hps
    kills = conn.execute("SELECT COUNT(*) FROM fights WHERE kill = 1").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM parses").fetchone()[0] == kills * 5
    assert conn.execute("SELECT COUNT(*) FROM reports WHERE rankings_synced_at IS NULL").fetchone()[0] == 0
    # a second sync fetches nothing new
    before = wcl.queries_made
    run_sync(conn, settings, wcl, rio, full=False, progress=lambda m: None)
    assert conn.execute("SELECT COUNT(*) FROM parses").fetchone()[0] == kills * 5
    assert not any("rankings" in str(c) for c in [])  # placeholder to keep structure clear
    assert wcl.queries_made > before
    perf = metrics.performance(conn, 46, 4, "6 Hour Team")
    names = [p["player"] for p in perf["players"]]
    assert names == ["Tagrik", "Sixer", "Elelena", "Bubonic"]  # sorted by average parse, pug filtered out
    assert perf["players"][0]["avg"] == 80.0 and perf["players"][1]["role"] == "healers" and perf["players"][1]["avg"] == 70.0
    assert perf["kills"] == 3 and perf["bosses"][0]["best_player"] == "Tagrik"
    with_pugs = metrics.performance(conn, 46, 4, "6 Hour Team", include_pugs=True)
    assert with_pugs["players"][0]["player"] == "Puggy"
    assert metrics.performance(conn, 46, 5, "6 Hour Team")["parses"] == 0
    assert metrics.parse_coverage(conn, 46)["synced"] == 3


def test_public_site(synced, tmp_path):
    conn, *_, settings = synced
    settings = settings.model_copy(update={"site_apply_url": "https://forms.example/apply", "site_recruiting": "Recruiting healers",
                                           "kt_state_url": "https://progress.example", "kt_state_secret": "s3cret"})
    data = public_summary(conn, settings)
    assert data["current"]["name"] == "The Venomous Abyss" and data["teams"] == ["CE Team", "6 Hour Team"]
    assert [t["team"] for t in data["current"]["teams"]] == ["CE Team", "6 Hour Team"]
    assert data["history"][0]["name"] == "Manaforge Omega" and data["history"][0]["ranks"]["mythic"]["world"] == 890
    assert data["links"]["raiderio"].endswith("/eu/draenor/Killing%20Time") and data["links"]["warcraftlogs"].endswith("/637454")
    html = render_public_page(conn, settings)
    for needle in ("Killing Time", "The Venomous Abyss", "CE Team", "6 Hour Team", "Recruiting healers", "forms.example/apply",
                   "Nek'zali the Soulcoiler", "Manaforge Omega", "Latest kills"):
        assert needle in html.replace("&#39;", "'"), needle
    # publishing PUTs the page to the Worker
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["secret"] = request.headers["x-kt-secret"]
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    assert state.publish_page(settings, html, http=httpx.Client(transport=httpx.MockTransport(handler))) is True
    assert seen["path"] == "/_internal/public" and seen["secret"] == "s3cret" and b"<html" in seen["body"]
    assert state.publish_page(Settings(_env_file=None), html) is False


def test_web_pages_with_teams(synced):
    conn, *_, settings = synced
    client = TestClient(create_app(settings, conn))
    for path in ["/t/ce-team/", "/t/6-hour-team/?tier=46&d=4", "/t/ce-team/history", "/t/6-hour-team/peers?d=4",
                 "/t/ce-team/peers?raid=manaforge-omega", "/t/6-hour-team/roster", "/t/6-hour-team/roster?tier=46&d=4&pugs=1",
                 "/t/ce-team/nights", "/t/6-hour-team/realm", "/status", "/public"]:
        r = client.get(path)
        assert r.status_code == 200, path
    # picking a team is remembered: / now goes straight to it
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/t/6-hour-team/"
    team_home = client.get("/t/6-hour-team/").text
    assert "3<span class=\"muted\">/3</span>" in team_home  # heroic clear is the team's best difficulty
    assert "Peer median" in team_home and "Internet Diff" not in team_home  # peers folded into the boss table, group list on /peers
    ce = client.get("/t/ce-team/?tier=46&d=5").text
    assert "Entombed Sentinels" in ce and "in progress" in ce
    guild = client.get("/t/guild/").text
    assert "Whole-guild" in guild or "Whole guild" in guild
    peers = client.get("/t/6-hour-team/peers?d=4").text
    assert "Guilds around our level" in peers and "Internet Diff" in peers
    roster = client.get("/t/6-hour-team/roster?tier=46&d=4").text
    assert "Tagrik" in roster and "Puggy" not in roster and "Attendance" in roster
    assert "Puggy" in client.get("/t/6-hour-team/roster?tier=46&d=4&pugs=1").text
    api = client.get("/api/peers/the-venomous-abyss?difficulty=5").json()
    assert api["peer_count"] == 2
    ros = client.get("/api/roster/46?difficulty=4&team=6+Hour+Team").json()
    assert ros["total_raids"] == 1 and ros["players"][0]["pct"] == 100.0
    pub = client.get("/api/public").json()
    assert pub["current"]["name"] == "The Venomous Abyss"


def test_migration_adds_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE reports (code TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, zone_id INTEGER, title TEXT, owner TEXT, start_time INTEGER NOT NULL, end_time INTEGER NOT NULL, fights_synced_at INTEGER)")
    old.commit()
    old.close()
    conn = connect(str(path))
    cols = {c["name"] for c in conn.execute("PRAGMA table_info('reports')")}
    assert "rankings_synced_at" in cols
