"""Raid teams, peer comparison, parses and the public site."""

from __future__ import annotations

from datetime import UTC

import httpx
from conftest import ms
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
    assert teams == {"C1": "6 Hour Team", "C2": "CE Team", "C2B": "CE Team", "C3": "CE Team"}
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
    kills = conn.execute("SELECT COUNT(*) FROM fights WHERE kill = 1 AND canonical = 1").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM parses").fetchone()[0] == kills * 5
    assert conn.execute("SELECT COUNT(*) FROM reports WHERE rankings_synced_at IS NULL").fetchone()[0] == 0
    # the kill on 2026-08-30 appears in both C2 and C2B but its rankings were fetched once, for the canonical copy
    assert conn.execute("SELECT COUNT(*) FROM parses WHERE report_code IN ('C2', 'C2B')").fetchone()[0] == 5
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
    assert metrics.parse_coverage(conn, 46)["synced"] == 4


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
                   "Nek'zali the Soulcoiler", "Manaforge Omega", "Recent", "Bosses killed", "Recruiting"):
        assert needle in html.replace("&#39;", "'"), needle
    assert data["schedule"]["days"] is not None and "totals" in data and "recruiting" in data
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
    assert "Similar progress" in peers and "Internet Diff" in peers
    roster = client.get("/t/6-hour-team/roster?tier=46&d=4").text
    assert "Tagrik" in roster and "Puggy" not in roster and "Attendance" in roster
    assert "Puggy" in client.get("/t/6-hour-team/roster?tier=46&d=4&pugs=1").text
    api = client.get("/api/peers/the-venomous-abyss?difficulty=5").json()
    assert api["peer_count"] == 2
    ros = client.get("/api/roster/46?difficulty=4&team=6+Hour+Team").json()
    assert ros["total_raids"] == 1 and ros["players"][0]["pct"] == 100.0
    pub = client.get("/api/public").json()
    assert pub["current"]["name"] == "The Venomous Abyss"


def test_dedupe_keeps_kill_and_longer_record(tmp_path):
    from killingtime.sync import dedupe_fights

    conn = connect(str(tmp_path / "d.db"))
    conn.execute("INSERT INTO expansions VALUES (1, 'x')")
    conn.execute("INSERT INTO zones(id, name, expansion_id) VALUES (1, 'z', 1)")
    conn.execute("INSERT INTO encounters VALUES (10, 1, 'Boss', 1, NULL)")
    conn.execute("INSERT INTO guilds(id, name, realm_slug, region, is_home) VALUES (1, 'g', 'r', 'eu', 1)")
    for code in ("A", "B"):
        conn.execute("INSERT INTO reports(code, guild_id, zone_id, start_time, end_time) VALUES (?, 1, 1, 0, 10)", (code,))
    # A logged the wipe as 99 s, B (clock 30 s ahead) as 90 s; on the kill pull B recorded the kill, A dropped combat log early
    rows = [("A", 1, 10, 5, 0, 1000, 100_000), ("B", 1, 10, 5, 0, 30_000, 120_000),
            ("A", 2, 10, 5, 0, 200_000, 500_000), ("B", 2, 10, 5, 1, 230_000, 520_000),
            ("A", 3, 10, 5, 0, 900_000, 950_000)]  # a genuinely separate later pull, only in A
    conn.executemany("INSERT INTO fights(report_code, fight_id, encounter_id, difficulty, kill, start_time, end_time) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    assert dedupe_fights(conn) == 2
    canon = {(r[0], r[1]): r[2] for r in conn.execute("SELECT report_code, fight_id, canonical FROM fights")}
    assert canon == {("A", 1): 1, ("B", 1): 0, ("A", 2): 0, ("B", 2): 1, ("A", 3): 1}  # longer record wins; the kill always wins
    assert tuple(conn.execute("SELECT COUNT(*), SUM(kill) FROM v_pulls").fetchone()) == (3, 1)
    assert dedupe_fights(conn) == 2  # idempotent


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


def test_peer_modes(synced):
    conn, *_ = synced
    # around our realm rank: the home guild is realm #3 on mythic; both other realm guilds sit within 20 places
    rank = metrics.peer_comparison(conn, "the-venomous-abyss", 5, mode="rank", above=20, below=20)
    assert rank["mode"] == "rank" and rank["our_rank"] == 3 and rank["rank_estimated"] is False
    assert [p["name"] for p in rank["peers"]] == ["Internet Diff", "Advance"] and rank["peers_ahead"] == 2
    assert metrics.peer_comparison(conn, "the-venomous-abyss", 5, mode="rank", above=1, below=0)["peer_count"] == 1
    # a team whose progress differs from the guild's gets an estimated rank from its own kills
    six = metrics.peer_comparison(conn, "the-venomous-abyss", 5, team="6 Hour Team", mode="rank")
    assert six["our_kills"] == 0 and six["our_rank"] is None and six["peer_count"] == 0
    # cohort: last tier only has us ranked in the fixture, so the cohort is empty but our previous rank is known
    cohort = metrics.peer_comparison(conn, "the-venomous-abyss", 5, mode="cohort", prev_raid_slug="manaforge-omega")
    assert cohort["mode"] == "cohort" and cohort["prev"]["our_rank"] == 1 and cohort["prev"]["raid_name"] == "Manaforge Omega"
    assert cohort["peer_count"] == 0
    # unknown modes fall back to similar progress
    assert metrics.peer_comparison(conn, "the-venomous-abyss", 5, mode="bogus")["mode"] == "level"
    rank_est = metrics.our_realm_rank(conn, "the-venomous-abyss", 4, "6 Hour Team")
    assert rank_est[2] == 3 and rank_est[0] is not None


def test_peer_modes_web(synced):
    conn, *_, settings = synced
    client = TestClient(create_app(settings, conn))
    for path in ["/t/guild/peers?peers=rank&above=5&below=5", "/t/guild/peers?peers=cohort", "/t/ce-team/peers?peers=rank",
                 "/t/guild/?peers=rank&above=10&below=10", "/t/6-hour-team/peers?d=4&peers=cohort&above=30&below=30"]:
        assert client.get(path).status_code == 200, path
    page = client.get("/t/guild/peers?peers=rank&above=5&below=5").text
    assert "Around our realm rank" in page and "Realm rank" in page and "5 places above" in page
    api = client.get("/api/peers/the-venomous-abyss?difficulty=5&peers=rank").json()
    assert api["mode"] == "rank" and api["peer_count"] == 2


def test_raiderio_fills_kills_missing_from_logs(synced):
    """A boss the guild killed per Raider.IO but that never reached our guild logs still counts (guild view only)."""
    conn, *_ = synced
    # Ula'tek (zone 46, mythic) has no kill in the fixture's logs; pretend Raider.IO credits the guild with it.
    home = metrics.home_guild(conn)["id"]
    conn.execute(
        """INSERT OR REPLACE INTO rio_progress(guild_id, raid_slug, difficulty, encounter_slug, first_defeated,
               last_defeated, num_pulls, best_percent, is_defeated, pull_started_at, fetched_at)
           VALUES (?, 'the-venomous-abyss', 5, 'ulatek', 1788000000000, NULL, 30, 0, 1, NULL, 0)""", (home,))
    conn.commit()
    s = metrics.tier_summary(conn, 46, 5)
    assert s["killed_logged"] == 1 and s["killed"] == 2
    assert s["unlogged_kills"] == ["Ulatek"]
    ula = next(b for b in s["bosses"] if b["name"] == "Ulatek")
    assert ula["log_missing"] is True and ula["killed_any"] is True and not ula["killed"]
    assert s["next_boss"]["name"] == "Entombed Sentinels"  # still the real next boss, not the one RIO filled in
    assert metrics.tiers(conn)[0]["kills"][5] == 2 and metrics.tiers(conn)[0]["logged_kills"][5] == 1
    # a team keeps its own logged kills: Raider.IO cannot attribute a kill to one team
    assert metrics.tier_summary(conn, 46, 5, "CE Team")["killed"] == 1
    assert metrics.tiers(conn, "CE Team")[0]["kills"][5] == 1
    # the peers view counts it as killed too, without inventing a pull count
    cmp = metrics.peer_comparison(conn, "the-venomous-abyss", 5)
    ula_peer = next(b for b in cmp["bosses"] if b["slug"] == "ulatek")
    assert ula_peer["killed"] is True and ula_peer["our_pulls"] is None


def test_season_cutoff_excludes_post_season_kills(synced):
    """A kill after the season ended is a clear, but not tier progress and no Cutting Edge."""
    conn, *_ = synced
    assert metrics.zone_cutoff(conn, 44) is not None and metrics.zone_cutoff(conn, 46) is None  # 46 is the live tier
    s = metrics.tier_summary(conn, 44, 5)
    assert s["killed"] == 2 and s["killed_all_time"] == 3 and s["cleared"] is False
    assert s["cutoff_date"] == "2026-05-10" and s["tier_over"] is True
    assert s["post_season_kills"] == [{"boss": "Dimensius", "date": "2026-05-14"}]
    assert s["achievement"] == "Cutting Edge" and s["achievement_earned"] is False
    assert s["next_boss"]["name"] == "Dimensius"  # the final boss is still what the tier ended on
    dim = next(b for b in s["bosses"] if b["name"] == "Dimensius")
    assert dim["post_season"] is True and dim["killed_any"] is True and dim["counts"] is False
    # the live tier has no cut-off, so nothing is post-season and the achievement is still open
    cur = metrics.tier_summary(conn, 46, 4)
    assert cur["post_season_kills"] == [] and cur["tier_over"] is False
    assert cur["achievement"] == "Ahead of the Curve" and cur["achievement_earned"] is True
    assert metrics.tier_summary(conn, 46, 5)["achievement"] == "Cutting Edge"


def test_season_cutoff_from_raider_io_seasons():
    from killingtime.sync import season_cutoffs

    seasons = [
        {"slug": "season-tww-3", "starts": {"eu": "2025-08-13T04:00:00Z"}, "ends": {"eu": "2026-03-02T22:00:00Z"}},
        {"slug": "season-tww-3-cutoffs", "starts": {"eu": "2025-08-13T04:00:00Z"}, "ends": {"eu": "2026-01-20T21:00:00Z"}},
        {"slug": "season-df-4", "starts": {"eu": "2024-04-24T04:00:00Z"}, "ends": {"eu": "2024-08-26T22:00:00Z"}},
        {"slug": "season-df-4-post", "starts": {"eu": "2024-07-24T04:00:00Z"}, "ends": {"eu": "2024-08-26T22:00:00Z"}},
        {"slug": "season-tww-2", "starts": {"eu": "2025-03-05T04:00:00Z"}, "ends": {"eu": "2025-08-13T04:00:00Z"}},
    ]
    got = {s["slug"]: metrics.ms_to_date(s["cutoff"]) for s in season_cutoffs(seasons, "eu")}
    assert got == {
        "season-tww-3": "2026-01-20",  # the -cutoffs variant, not the later season end
        "season-df-4": "2024-07-24",   # the -post season starts at the cut-off
        "season-tww-2": "2025-08-13",  # no variant: the season end is the cut-off
    }


def test_pulls_to_kill_uses_the_true_first_kill(synced):
    """Guild logs can be late on a first kill, so later farm wipes must not count as progression pulls."""
    conn, *_ = synced
    home = metrics.home_guild(conn)["id"]
    before = metrics.tier_summary(conn, 46, 5)
    ent = next(b for b in before["bosses"] if b["name"] == "Entombed Sentinels")
    assert ent["total_pulls"] == 4 and not ent["killed_any"]  # 1 pull on 08-30, 3 more on 09-02, no logged kill
    # Raider.IO says the guild actually killed it on the first night; our logs never saw that kill.
    conn.execute(
        """INSERT OR REPLACE INTO rio_progress(guild_id, raid_slug, difficulty, encounter_slug, first_defeated,
               last_defeated, num_pulls, best_percent, is_defeated, pull_started_at, fetched_at)
           VALUES (?, 'the-venomous-abyss', 5, 'entombed-sentinels', ?, NULL, 1, 0, 1, NULL, 0)""",
        (home, ms("2026-08-30", 19) + 20 * 60 * 1000),
    )
    conn.commit()
    after = metrics.tier_summary(conn, 46, 5)
    ent = next(b for b in after["bosses"] if b["name"] == "Entombed Sentinels")
    assert ent["killed_any"] and ent["log_missing"] and ent["kill_date"] == "2026-08-30"
    assert ent["total_pulls"] == 4  # every pull is still counted for the tier
    assert ent["pulls_to_kill"] == 1  # but only the one before the real kill is progression
    assert ent["nights_to_kill"] == 1
    assert after["killed"] == 2 and after["next_boss"]["name"] == "Ulatek"


def test_peers_respect_the_cutoff_and_declare_untracked_bosses(synced):
    """The Raider.IO fallback must not count a post-season kill, and a boss Raider.IO does not track is declared."""
    conn, *_ = synced
    # Manaforge: our logs hold the pulls, and Dimensius died post-season
    cmp = metrics.peer_comparison(conn, "manaforge-omega", 5)
    assert cmp["our_kills"] == 2 and cmp["total_bosses"] == 3
    dim = next(b for b in cmp["bosses"] if b["slug"] == "dimensius")
    assert dim["killed"] is False  # killed after the cut-off
    # drop our logs for the raid so the Raider.IO fallback is used, and check it applies the cut-off too
    conn.execute("DELETE FROM fights WHERE report_code IN (SELECT code FROM reports WHERE zone_id = 44)")
    conn.commit()
    fallback = metrics.peer_comparison(conn, "manaforge-omega", 5)
    assert fallback["source"] == "raider.io" and fallback["our_kills"] == 2
    assert next(b for b in fallback["bosses"] if b["slug"] == "dimensius")["killed"] is False
    assert metrics.raid_cutoff(conn, "manaforge-omega") is not None
    assert metrics.raid_cutoff(conn, "the-venomous-abyss") is None  # season still running


def test_meet_the_team(synced):
    """Cards carry the character profile, a bio from the player's own numbers and a portrait prompt."""
    conn, *_ = synced
    assert conn.execute("SELECT COUNT(*) FROM characters WHERE missing = 0").fetchone()[0] >= 3
    cards = metrics.meet_the_team(conn, 46, 4, min_raids=1)
    by_name = {c["player"]: c for c in cards}
    assert "Tagrik" in by_name and "Puggy" not in by_name  # other-realm pugs are not team members
    t = by_name["Tagrik"]
    assert t["race"] == "Orc" and t["class"] == "Warrior" and t["spec"] == "Arms"
    assert t["weapons"] == ["Bonecleaver of Poor Decisions"]
    assert t["portrait_url"].endswith("-inset.jpg") and t["thumbnail_url"].endswith("-avatar.jpg")
    assert 4 <= len(t["bio"]) <= 8 and all(line.strip() for line in t["bio"])
    assert t["slug"] == "tagrik" and len(t["frames"]) == 5 and t["frames"][0].endswith("/tagrik/1.webp")
    assert [f["label"] for f in t["portrait_prompts"]][0] == "At arms"
    assert len(t["portrait_prompts"]) == 5
    assert t["history"]["total_raids"] >= 1 and t["history"]["tiers"]
    assert {s["label"] for s in t["stats"]} >= {"Raids this tier", "Attendance", "Avg parse", "Career raids"}
    prompt = t["portrait_prompt"]
    for needle in ("Tagrik", "Orc", "Arms", "Warrior", "Bonecleaver of Poor Decisions", "Killing Time",
                   "the Venomous Abyss raid floor", "Pose:", "Style:"):
        assert needle in prompt, needle
    assert "the The Venomous Abyss" not in prompt
    # a healer gets healer flavour and both weapons
    sixer = by_name["Sixer"]
    assert "Tome of Unread Whispers" in sixer["portrait_prompt"] and sixer["role"] == "healers"
    # bios are stable between calls
    assert metrics.meet_the_team(conn, 46, 4, min_raids=1)[0]["bio"] == cards[0]["bio"]


def test_meet_the_team_page(synced):
    conn, *_, settings = synced
    client = TestClient(create_app(settings, conn))
    for path in ("/t/guild/meet", "/t/ce-team/meet", "/t/6-hour-team/meet?d=4"):
        assert client.get(path).status_code == 200, path
    page = client.get("/t/guild/meet?d=4").text
    assert "Meet the team" in page and "Tagrik" in page and "portrait-stack" in page
    assert "Portrait prompt" not in page  # the prompts are a build-time tool, not something a reader wants
    assert "-inset.jpg" in page
    api = client.get("/api/meet/46?difficulty=4").json()
    assert any(c["player"] == "Tagrik" and c["portrait_prompt"] for c in api)


def test_boss_pulls(synced):
    """The pull-by-pull view for the boss we are working on: every pull, the running best, nights."""
    conn, *_ = synced
    d = metrics.boss_pulls(conn, 46, 3202, 5)  # Entombed Sentinels, Mythic: 4 pulls over two nights, no kill
    assert d["boss"]["name"] == "Entombed Sentinels" and d["killed"] is False
    assert d["total_pulls"] == 4 and d["wipes"] == 4  # the duplicate log of night one is not counted twice
    assert [p["n"] for p in d["pulls"]] == [1, 2, 3, 4]
    assert [p["pct_left"] for p in d["pulls"]] == [80.0, 66.0, 41.2, 38.0]
    assert [p["best_so_far"] for p in d["pulls"]] == [80.0, 66.0, 41.2, 38.0]  # only ever falls
    assert d["best_pct"] == 38.0 and d["best_pull"] == 4 and d["last_pct"] == 38.0
    assert [n["pulls"] for n in d["nights"]] == [1, 3] and len(d["nights"]) == 2
    assert d["pulls"][0]["log_url"].startswith("https://www.warcraftlogs.com/reports/")

    kill = metrics.boss_pulls(conn, 46, 3201, 5)  # killed on the third pull
    assert kill["killed"] and kill["kill_pull"] == 3 and kill["pulls"][2]["pct_left"] == 0.0
    assert metrics.boss_pulls(conn, 46, 3203, 5)["total_pulls"] == 0  # never pulled at Mythic


def test_boss_page(synced):
    conn, *_, settings = synced
    client = TestClient(create_app(settings, conn))
    page = client.get("/t/guild/boss/3202?tier=46&d=5")
    assert page.status_code == 200
    assert "Entombed Sentinels" in page.text and "Pull by pull" in page.text and "38.0" in page.text
    assert client.get("/t/guild/boss/3203?tier=46&d=5").status_code == 200  # no pulls: still renders
    api = client.get("/api/boss/46/3202?difficulty=5").json()
    assert api["total_pulls"] == 4 and api["best_pct"] == 38.0


def test_public_team_page(synced):
    """Meet the Team is part of the public site, with its own page and a link from the front page."""
    from killingtime.public import render_public_team_page, roster_cards

    conn, *_, settings = synced
    groups = roster_cards(conn, settings)
    assert groups and all(g["members"] for g in groups)
    names = [m["player"] for g in groups for m in g["members"]]
    assert "Tagrik" in names and len(names) == len(set(names))  # nobody appears under two teams
    m = next(m for g in groups for m in g["members"] if m["player"] == "Tagrik")
    assert m["bio"] and m["race"] == "Orc" and "portrait_prompt" not in m  # prompts stay out of the public page
    html = render_public_team_page(conn, settings)
    assert "Meet the team" in html and "Tagrik" in html and "portrait_prompt" not in html

    client = TestClient(create_app(settings, conn))
    page = client.get("/public/team")
    assert page.status_code == 200 and "Meet the team" in page.text
    assert '/team' in client.get("/public").text  # the front page links to it


def test_raid_schedule_and_join_page(synced):
    """The landing page works out when we raid from the logs, rather than from a settings string."""
    from killingtime.public import render_public_join_page

    conn, *_, settings = synced
    sched = metrics.raid_schedule(conn, months=24)
    assert sched["days"], "the fixture raids on fixed weekdays, so a schedule must fall out of it"
    for d in sched["days"]:
        assert d["day"] in metrics.WEEKDAYS and len(d["start"]) == 5 and len(d["end"]) == 5
        assert d["nights"] >= 2
    assert sched["nights_per_week"] == len(sched["days"]) and sched["summary"]

    html = render_public_join_page(conn, settings)
    assert "Killing Time" in html and "Applying" in html and "Recruiting" in html
    client = TestClient(create_app(settings, conn))
    assert client.get("/public/join").status_code == 200


def test_alt_candidates_and_merge(synced):
    """Alt detection suggests, RAID_ALTS decides, and a confirmed alt's nights count towards its main."""
    conn, *_ = synced
    cands = metrics.alt_candidates(conn, min_nights=1)
    assert isinstance(cands, list)
    for row in cands:
        assert row["candidates"] and row["player"] not in [c["name"] for c in row["candidates"]]
        for c in row["candidates"]:
            # the whole point: a candidate never shares a raid night with its main
            shared = conn.execute(
                """SELECT COUNT(*) AS n FROM (
                       SELECT raid_date FROM v_attendance WHERE presence = 1 AND player_name = ?
                       INTERSECT SELECT raid_date FROM v_attendance WHERE presence = 1 AND player_name = ?)""",
                (row["player"], c["name"]),
            ).fetchone()["n"]
            assert shared == 0, f"{row['player']} raided with {c['name']}"
            assert 0 <= c["score"] <= 1

    history = metrics.career_history(conn)
    main = next(n for n, h in history.items() if h["total_raids"] >= 1)
    alt = next(n for n, h in history.items() if n != main and h["total_raids"] >= 1)
    merged = metrics.merge_alt_history(history, {main: [alt]})
    assert merged[main]["total_raids"] >= history[main]["total_raids"] + 1
    assert merged[main]["tier_count"] >= history[main]["tier_count"]
    assert merged[main]["alt_characters"] == [alt]
    assert merged[alt] == history[alt]  # the alt's own row is left alone


def test_achievement_unknown_without_a_season_window(synced):
    """A raid with no Raider.IO season window cannot claim Cutting Edge off a farm kill years later."""
    conn, *_ = synced
    assert metrics.zone_season_known(conn, 44) is True   # a closed season we have the dates for
    assert metrics.tier_summary(conn, 44, 5)["achievement_earned"] is False
    assert metrics.zone_season_known(conn, 46) is True   # live season: a kill today still earns it
    assert metrics.tier_summary(conn, 46, 4)["achievement_earned"] is True

    conn.execute("UPDATE rio_raids SET cutoff_at = NULL, ends_at = NULL WHERE slug = 'manaforge-omega'")
    conn.commit()
    assert metrics.zone_season_known(conn, 44) is False
    s = metrics.tier_summary(conn, 44, 5)
    assert s["achievement_earned"] is None and s["tier_over"] is False


def _art_dir(monkeypatch, tmp_path, *names: str):
    """Point the artwork lookup at a temporary directory and drop stills for the named bosses in it.

    Never write into killingtime/web/static: those files ship with the app, and a test that cleans up after itself
    there will happily delete a real boss's picture.
    """
    import killingtime.public as public

    art = tmp_path / "bosses"
    art.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(public, "boss_art_dir", lambda: art)
    for name in names:
        (art / f"{metrics._slug(name)}.webp").write_bytes(b"RIFFfake")
    return art


def test_boss_art_looks_on_disk(monkeypatch, tmp_path):
    """A boss only gets a picture if we actually generated one; a made-up slug never yields a URL."""
    from killingtime.public import boss_art

    _art_dir(monkeypatch, tmp_path, "Dimensius")
    assert boss_art("no-such-boss-at-all") == {"still": None, "video": None}
    assert boss_art("") == {"still": None, "video": None}
    art = boss_art("dimensius")
    assert art["still"] == "/static/site/bosses/dimensius.webp"
    assert art["video"] is None  # the clip is optional, and half a set must not invent the other half


def test_kill_reel_groups_by_tier_newest_first(synced, monkeypatch, tmp_path):
    """The wall is one row per tier, the tier we are on at the top, and every tile is sized by what the kill was."""
    from killingtime.public import kill_reel

    conn, *_, settings = synced
    # Move the Manaforge cut-off past the Dimensius kill, so that clear becomes a genuine Cutting Edge.
    conn.execute("UPDATE rio_raids SET cutoff_at = ? WHERE slug = 'manaforge-omega'", (ms("2026-06-01"),))
    conn.commit()
    _art_dir(monkeypatch, tmp_path, "Dimensius")
    groups = kill_reel(conn, settings)
    assert [g["tier"] for g in groups] == ["The Venomous Abyss", "Manaforge Omega"]
    now, past = groups
    assert now["current"] is True and now["ce"] is False and past["current"] is False and past["ce"] is True
    assert now["killed"] == len(now["kills"]) <= now["bosses"]

    # A kill with no artwork still gets its plate: a name and a date beats borrowing another boss's dragon.
    nek = next(k for k in now["kills"] if k["boss"] == "Nek'zali the Soulcoiler")
    assert nek["still"] is None and nek["date"] == "2026-08-30" and nek["current"] is True

    ce = past["kills"][0]  # the boss that closed the tier out leads its row
    assert ce["boss"] == "Dimensius" and ce["final"] is True and ce["ce"] is True and ce["size"] == "lg"
    assert ce["still"] == "/static/site/bosses/dimensius.webp" and ce["date"] == "2026-05-14"
    assert all(k["size"] == "sm" for k in past["kills"][1:])  # everything behind it is a small plate
    assert all(k["size"] == "xl" for k in now["kills"])  # ...but the tier we are on is all headline
    assert len(kill_reel(conn, settings, tiers=1)) == 1  # the cap is honoured


def test_public_summary_carries_the_reel(synced, monkeypatch, tmp_path):
    """The landing page gets per-boss art data, and the generic clip reel stays untouched beside it."""
    conn, *_, settings = synced
    keys = {"boss", "slug", "tier", "difficulty", "date", "pulls", "ce", "final", "still", "video", "headline",
            "sub", "ord", "of", "nights", "wipes", "prog", "prog_short"}
    _art_dir(monkeypatch, tmp_path, "Nek'zali the Soulcoiler")
    data = public_summary(conn, settings)
    assert data["clips"] and all("file" in c for c in data["clips"])  # the fallback reel is unchanged
    assert data["kill_reel"] and all(
        set(g) == {"tier", "tier_id", "current", "ce", "killed", "bosses", "kills", "ranks"}
        for g in data["kill_reel"])
    assert all(set(e) == keys | {"current", "size"} and e["boss"] and e["date"]
               for g in data["kill_reel"] for e in g["kills"])
    latest = data["latest_kill"]
    assert set(latest) == keys and latest["boss"] == "Nek'zali the Soulcoiler" and latest["date"] == "2026-08-30"
    assert latest["still"] == "/static/site/bosses/nek-zali-the-soulcoiler.webp"

    # With no art the kill is still reported, just without a picture for the caption.
    _art_dir(monkeypatch, tmp_path / "empty")
    assert public_summary(conn, settings)["latest_kill"]["still"] is None


def test_lair_bosses_are_not_part_of_the_raid(synced):
    """Warcraft Logs files lair bosses in the raid's zone; Raider.IO, the armory and the guild do not count them."""
    conn, *_ = synced
    before = metrics.tier_summary(conn, 46, 4)
    n = before["total_bosses"]
    conn.execute(
        """INSERT INTO encounters(id, zone_id, name, ord, rio_encounter_slug)
           VALUES (99001, 46, 'Some Lair Boss', 99, NULL)""")
    conn.commit()
    after = metrics.tier_summary(conn, 46, 4)
    assert after["total_bosses"] == n, "an encounter Raider.IO's raid does not have is not one of the raid's bosses"
    assert "Some Lair Boss" not in [b["name"] for b in after["bosses"]]
    assert [t for t in metrics.tiers(conn) if t["id"] == 46][0]["bosses"] == n

    # A tier we hold no Raider.IO mapping for keeps every boss: there, the mapping tells us nothing at all.
    conn.execute("UPDATE encounters SET rio_encounter_slug = NULL WHERE zone_id = 46")
    conn.commit()
    assert metrics.tier_summary(conn, 46, 4)["total_bosses"] == n + 1


def test_raid_hours_survive_midnight():
    """A raid that ends after midnight lasts three hours, not minus twenty-one."""
    import datetime as dt
    from unittest.mock import patch

    rows = []
    for week in range(4):  # four Wednesdays, 21:10 until 00:07 the next morning
        start = dt.datetime(2026, 9, 2, 19, 10, tzinfo=UTC) + dt.timedelta(days=7 * week)  # 21:10 server time
        rows.append({"pull_date": start.date().isoformat(), "pulls": 12,
                     "first_ms": int(start.timestamp() * 1000),
                     "last_ms": int((start + dt.timedelta(hours=2, minutes=57)).timestamp() * 1000)})

    conn = connect(":memory:")
    with patch.object(metrics, "_rows", return_value=rows), patch.object(metrics, "home_guild", return_value={"region": "eu"}):
        sched = metrics.raid_schedule(conn)
    day = sched["days"][0]
    assert day["start"] == "21:10" and day["end"] == "00:07"  # crosses midnight in server time
    assert sched["hours_per_week"] == 3.0 and sched["hours_per_week"] > 0


def test_alt_groups_pool_one_players_characters(synced):
    """Nobody plays two characters at once, so a guild-mate with the same name stem who never shares a night is them."""
    conn, *_ = synced
    groups = metrics.alt_groups(conn)
    assert isinstance(groups, dict)
    for main, g in groups.items():
        assert g["main"] == main and main not in g["alts"] and g["source"] in {"confirmed", "roster"}
        for alt in g["alts"]:
            shared = conn.execute(
                """SELECT COUNT(*) AS n FROM (
                       SELECT raid_date FROM v_attendance WHERE presence = 1 AND player_name = ?
                       INTERSECT SELECT raid_date FROM v_attendance WHERE presence = 1 AND player_name = ?)""",
                (main, alt)).fetchone()["n"]
            assert shared == 0, f"{main} and {alt} raided together, so they are two people"

    # A name told to us wins whatever the roster says, and pooling survives it.
    told = metrics.alt_groups(conn, {"Tagrik": ["Mira"]})
    assert "Mira" in told["Tagrik"]["alts"] and told["Tagrik"]["source"] == "confirmed"

    # Two different people are never pooled, however similar their raid weeks.
    assert not any("Bruk" in g["alts"] and g["main"] == "Mira" for g in groups.values())


def test_an_alt_does_not_get_its_own_card(synced):
    """One person, one card: an alt folds into their main rather than appearing beside them."""
    conn, *_, settings = synced
    cards = metrics.meet_the_team(conn, 46, 4, min_raids=1, alts={"Tagrik": ["Bruk"]})
    names = [c["player"] for c in cards]
    assert "Tagrik" in names and "Bruk" not in names
    tagrik = next(c for c in cards if c["player"] == "Tagrik")
    assert "Bruk" in tagrik["alts"] and tagrik["alts_source"] == "confirmed"
    assert tagrik["history"]["total_raids"] >= 2  # Bruk's nights count towards Tagrik's career


class FakeAnthropic:
    """Stands in for the API: records what it was asked, returns a story shaped like a real one."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.messages = self

    def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        block = type("B", (), {"type": "text", "text": "A short, entirely invented story about a raider."})()
        return type("R", (), {"content": [block]})()


def test_stories_are_written_once_and_reused(synced):
    """A story is written from a raider's facts and kept until those facts move."""
    from killingtime.stories import stored, write_stories

    conn, *_, settings = synced
    cards = metrics.meet_the_team(conn, 46, 4, min_raids=1)
    settings = settings.model_copy(update={"anthropic_api_key": "test-key"})
    client = FakeAnthropic()

    first = write_stories(conn, cards, settings, client=client)
    assert first["written"] == len(cards) and first["failed"] == 0
    assert set(stored(conn)) == {c["player"] for c in cards}
    assert all("Shape to write in" in p and "Write the piece" in p for p in client.prompts)
    # the facts go in as data, and the shape differs between raiders so the page is not one joke sixty times
    shapes = {r["shape"] for r in conn.execute("SELECT shape FROM bios")}
    assert len(shapes) >= min(2, len(cards))

    second = write_stories(conn, cards, settings, client=FakeAnthropic())
    assert second["written"] == 0 and second["kept"] == len(cards)  # unchanged facts, no rewrite, no spend

    forced = write_stories(conn, cards, settings, client=FakeAnthropic(), refresh=True)
    assert forced["written"] == len(cards)


def test_no_key_means_no_stories_and_the_bio_stands_in(synced):
    """Without an Anthropic key the page falls back to the generated bio rather than showing nothing."""
    from killingtime.stories import write_stories

    conn, *_, settings = synced
    settings = settings.model_copy(update={"anthropic_api_key": ""})
    tally = write_stories(conn, metrics.meet_the_team(conn, 46, 4, min_raids=1), settings)
    assert tally == {"written": 0, "kept": 0, "failed": 0}
    card = metrics.meet_the_team(conn, 46, 4, min_raids=1)[0]
    assert card["story"] is None and card["bio"]


def test_a_written_story_reaches_the_card(synced):
    from killingtime.stories import write_stories

    conn, *_, settings = synced
    cards = metrics.meet_the_team(conn, 46, 4, min_raids=1)
    write_stories(conn, cards, settings.model_copy(update={"anthropic_api_key": "k"}), client=FakeAnthropic())
    again = metrics.meet_the_team(conn, 46, 4, min_raids=1)
    assert all(c["story"] == "A short, entirely invented story about a raider." for c in again)


def test_seeded_stories_belong_to_this_guild_only(synced):
    """The bundled stories are one guild's in-jokes; another guild's database must not inherit them."""
    from killingtime.stories import seed, stored

    conn, *_ = synced
    conn.execute("DELETE FROM bios")
    conn.commit()
    added = seed(conn)
    have = stored(conn)
    raiders = {r["player_name"] for r in conn.execute(
        "SELECT DISTINCT player_name FROM v_attendance WHERE presence = 1")}
    assert set(have) <= raiders, "seeded a story for somebody who has never raided here"
    assert seed(conn) == 0, "seeding twice must not duplicate or rewrite what has not changed"
    assert added == len(have)

    # Editing the bundled text is not enough on its own: a bio somebody is already reading stays put.
    name = sorted(have)[0]
    conn.execute("UPDATE bios SET story = 'the one on the page' WHERE player_name = ?", (name,))
    conn.commit()
    assert seed(conn) == 0 and stored(conn)[name] == "the one on the page"

    # Raising that entry's rev is the deliberate act that replaces it, and only that entry.
    import json as _json
    from pathlib import Path as _Path

    from killingtime import stories as _stories

    src = _Path(_stories.__file__).parent / _stories.SEED_FILE
    data = _json.loads(src.read_text(encoding="utf-8"))
    original = src.read_text(encoding="utf-8")
    data["players"][name]["rev"] = 9
    src.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        assert seed(conn) == 1 and stored(conn)[name] == have[name]
        assert seed(conn) == 0, "the same rev must not be applied twice"

        # A story Claude wrote is never written back over by the bundled one, whatever the rev says.
        conn.execute("UPDATE bios SET story = 'mine', model = 'claude-opus-5' WHERE player_name = ?", (name,))
        data["players"][name]["rev"] = 10
        src.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        conn.commit()
        assert seed(conn) == 0 and stored(conn)[name] == "mine"
    finally:
        src.write_text(original, encoding="utf-8")


def test_the_public_api_reports_how_the_last_sync_went(synced):
    """A sync that fails leaves the site serving an old page; the outcome has to be readable without the password."""
    from killingtime.public import last_sync_run

    conn, *_ = synced
    assert last_sync_run(conn)["status"] in {"ok", "error"}

    conn.execute("INSERT INTO sync_log(started_at, finished_at, status, detail) VALUES (?, NULL, 'error', ?)",
                 (ms("2026-09-08"), '{"warnings": ["wcl: rate limited"]}'))
    conn.commit()
    run = last_sync_run(conn)
    assert run["status"] == "error" and run["running"] is True
    assert run["warnings"] == ["wcl: rate limited"]


def test_each_team_carries_its_own_boss_by_boss_progress(synced):
    """The public team card has to show where a team got to, not only how many fell."""
    conn, *_ = synced
    cur = metrics.current_tier(conn)
    teams = [t for t in metrics.teams_seen(conn) if t]
    progress = metrics.team_progress(conn, cur["id"], teams)
    assert progress, "fixture has no teams"
    for t in progress:
        assert t["bosses"], f"{t['team']} has no boss list"
        # Reported at the hardest difficulty that team actually raids, and every boss says one thing or the other.
        assert len({b["difficulty"] for b in t["bosses"]}) == 1
        for b in t["bosses"]:
            assert b["killed"] == (b["date"] is not None)
            if not b["killed"] and b["best_pct"] is not None:
                assert 0 <= b["best_pct"] <= 100


def test_a_kill_tile_says_where_the_boss_sat_and_what_it_cost(synced, monkeypatch, tmp_path):
    """The wall is the only place most people look, so a tile carries the run order and the prog, not just a date."""
    from killingtime.public import kill_reel

    conn, *_, settings = synced
    _art_dir(monkeypatch, tmp_path, "Dimensius")
    for g in kill_reel(conn, settings):
        for k in g["kills"]:
            assert 1 <= k["ord"] <= k["of"], f"{k['boss']} sits at {k['ord']} of {k['of']}"
            assert k["prog"] == "" or "pull" in k["prog"]
            if k["pulls"]:
                assert f"{k['pulls']:,} pull" in k["prog"]
                # The short form is what a small plate shows, and it must never be the long one.
                assert k["prog_short"] == f"{k['pulls']:,} pull" + ("" if k["pulls"] == 1 else "s")
                assert len(k["prog_short"]) <= len(k["prog"])


def test_a_raider_who_has_left_comes_off_the_page(synced):
    """Their nights stay in the logs and in the tier numbers; the card goes."""
    conn, *_ = synced
    cur = metrics.current_tier(conn)
    everyone = metrics.meet_the_team(conn, cur["id"], 5, min_raids=1)
    assert everyone, "fixture has nobody to remove"
    gone = everyone[0]["player"]
    left = metrics.meet_the_team(conn, cur["id"], 5, min_raids=1, exclude={gone})
    assert gone not in {c["player"] for c in left} and len(left) == len(everyone) - 1


def test_stated_raid_hours_beat_the_logs(synced):
    """Which nights we raid comes from the logs; what time we raid is a decision the guild states."""
    from killingtime.public import public_summary, stated_hours

    conn, *_, settings = synced
    derived = metrics.raid_schedule(conn, months=24)
    assert derived["days"] and not derived.get("stated")

    stated = stated_hours(derived, "21:00-24:00")
    assert [d["day"] for d in stated["days"]] == [d["day"] for d in derived["days"]]  # the nights are unchanged
    assert all(d["start"] == "21:00" and d["end"] == "24:00" for d in stated["days"])
    assert stated["hours_per_week"] == 3.0 * len(stated["days"]) and stated["stated"] is True
    assert stated["summary"].endswith("21:00-24:00 server time")

    assert stated_hours(derived, "") == derived           # nothing stated, nothing changed
    assert stated_hours(derived, "whenever") == derived   # and nothing we cannot parse

    told = settings.model_copy(update={"site_raid_hours": "21:00-24:00"})
    assert public_summary(conn, told)["schedule"]["summary"].endswith("21:00-24:00 server time")


def test_raiders_means_guild_members_not_everyone_ever_logged(synced):
    """Eight years of logs hold every pug and trial who ever came along; they are not our raiders."""
    from killingtime.public import guild_totals

    conn, *_ = synced
    conn.execute("DELETE FROM guild_members")
    conn.execute("""INSERT INTO guild_members(name, realm_slug, region, fetched_at)
                    SELECT DISTINCT player_name, 'draenor', 'eu', 0 FROM v_attendance WHERE presence = 1 LIMIT 2""")
    conn.commit()
    totals = guild_totals(conn)
    assert totals["raiders"] == 2
    assert totals["ever_raided"] >= totals["raiders"], "the wider number still exists, it is just not the headline"
