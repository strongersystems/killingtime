"""End-to-end sync into SQLite from the fake clients, then check the derived metrics."""

from __future__ import annotations

from killingtime import metrics
from killingtime.config import parse_rival_guilds
from killingtime.sync import match_name, run_sync, slugify


def test_parse_rivals():
    refs = parse_rival_guilds("Internet Diff@draenor/eu; Advance@Twisting Nether/EU")
    assert refs[0].name == "Internet Diff" and refs[0].realm_slug == "draenor" and refs[0].region == "eu"
    assert refs[1].realm_slug == "twisting-nether"


def test_slug_matching():
    assert slugify("Nek'zali the Soulcoiler") == "nekzali-the-soulcoiler"
    cands = {"fallenking-salhadaar": "Fallen King Salhadaar", "vorasius": "Vorasius", "the-coiled-altar": "The Coiled Altar"}
    assert match_name("Fallen King Salhadaar", cands) == "fallenking-salhadaar"
    assert match_name("Coiled Altar", cands) == "the-coiled-altar"
    assert match_name("Totally Different Boss", cands) is None


def test_sync_populates_tables(synced):
    conn, wcl, rio, settings = synced
    assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 7  # dungeon report ignored
    # trash fights (encounterID 0) are dropped
    assert conn.execute("SELECT COUNT(*) FROM fights WHERE encounter_id = 0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fights").fetchone()[0] == 4 + 6 + 6 + 5 + 4 + 4 + 3
    home = metrics.home_guild(conn)
    assert home["wcl_id"] == 637454 and home["faction"] == "horde" and home["is_home"] == 1
    # attendance for the current tier only
    assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 6
    # zone rankings: frozen zone raised a WCLError and was skipped, current zone stored
    ranks = conn.execute("SELECT metric, world_rank FROM wcl_zone_rankings WHERE zone_id = 46 ORDER BY metric").fetchall()
    assert [(r["metric"], r["world_rank"]) for r in ranks] == [("completeRaidSpeed", 1200), ("progress", 890)]
    # raider.io: static data resolved by probing expansion ids, zones mapped by name
    assert conn.execute("SELECT rio_raid_slug FROM zones WHERE id = 46").fetchone()[0] == "the-venomous-abyss"
    assert conn.execute("SELECT rio_raid_slug FROM zones WHERE id = 44").fetchone()[0] == "manaforge-omega"
    assert conn.execute("SELECT COUNT(*) FROM encounters WHERE rio_encounter_slug IS NULL").fetchone()[0] == 0
    # rival flagged, missing rival produced a warning not a crash
    assert conn.execute("SELECT is_rival FROM guilds WHERE name = 'Internet Diff'").fetchone()[0] == 1
    # world boss raid (1 encounter) is not scanned
    scanned = {c[1][0] for c in rio.calls if c[0] == "rankings"}
    assert scanned == {"the-venomous-abyss", "manaforge-omega"}
    log = conn.execute("SELECT status FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["status"] == "ok"


def test_incremental_sync_is_idempotent(synced):
    conn, wcl, rio, settings = synced
    before = conn.execute("SELECT COUNT(*) FROM fights").fetchone()[0]
    stats = run_sync(conn, settings, wcl, rio, full=False, progress=lambda m: None)
    assert conn.execute("SELECT COUNT(*) FROM fights").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 7
    assert stats.fights == 0  # nothing re-fetched: all reports already had fights_synced_at


def test_first_kills_view(synced):
    conn, *_ = synced
    rows = {r["encounter_name"]: dict(r) for r in conn.execute(
        "SELECT * FROM v_first_kills WHERE zone_id = 44 AND difficulty = 5")}
    assert rows["Plexus Sentinel"]["pulls_to_kill"] == 4
    assert rows["Plexus Sentinel"]["wipes_before_kill"] == 3
    assert rows["Plexus Sentinel"]["best_wipe_pct"] == 3.0
    assert rows["Loom'ithar"]["pulls_to_kill"] == 6 and rows["Loom'ithar"]["nights_to_kill"] == 1
    # Dimensius: 6 wipes night 1, 3 wipes + kill night 2 -> 10 pulls to first kill; the re-kill is excluded
    assert rows["Dimensius"]["pulls_to_kill"] == 10 and rows["Dimensius"]["nights_to_kill"] == 2
    assert rows["Dimensius"]["first_kill_date"] == "2026-05-14"
    cur = {r["encounter_name"]: dict(r) for r in conn.execute(
        "SELECT * FROM v_first_kills WHERE zone_id = 46 AND difficulty = 5")}
    assert cur["Nek'zali the Soulcoiler"]["killed"] == 1 and cur["Nek'zali the Soulcoiler"]["pulls_to_kill"] == 3
    assert cur["Entombed Sentinels"]["killed"] == 0 and cur["Entombed Sentinels"]["pulls_to_kill"] == 4
    assert cur["Entombed Sentinels"]["best_wipe_pct"] == 38.0
    assert "Ulatek" not in cur


def test_tier_metrics(synced):
    conn, *_ = synced
    tiers = metrics.tiers(conn)
    assert [t["id"] for t in tiers] == [46, 44]
    assert tiers[1]["kills"] == {5: 3} and tiers[1]["summary"] == "3/3 M"
    assert tiers[0]["kills"] == {5: 1, 4: 3}
    s = metrics.tier_summary(conn, 46, 5)
    assert s["killed"] == 1 and s["total_bosses"] == 3 and s["pulls"] == 7 and s["wipes"] == 6
    assert s["next_boss"]["name"] == "Entombed Sentinels" and s["next_boss"]["best_pct"] == 38.0
    assert s["nights"] == 2
    tl = metrics.progress_timeline(conn, 44, 5)
    assert [p["kills"] for p in tl] == [1, 2, 3] and tl[-1]["day"] == 9.0
    cmp = metrics.tier_comparison(conn, 5)
    prev = next(c for c in cmp if c["zone_id"] == 44)
    assert [b["cum_pulls"] for b in prev["bosses"]] == [4, 10, 20]
    assert prev["days_to_latest_kill"] == 9.0
    nights = metrics.raid_nights(conn, 44)
    assert nights[0]["pull_date"] == "2026-05-14" and nights[0]["kills"] == 2 and nights[0]["wipes"] == 3
    att = metrics.attendance_summary(conn, 46)
    assert att["total_raids"] == 3
    assert att["players"][0]["player_name"] == "Tagrik" and att["players"][0]["pct"] == 100.0
    bub = next(p for p in att["players"] if p["player_name"] == "Bubonic")
    assert bub["raids"] == 1  # presence 2 (bench) does not count


def test_rival_metrics(synced):
    conn, *_ = synced
    raids = metrics.rio_raids_for_zone(conn)
    assert raids[0]["slug"] == "the-venomous-abyss" and raids[0]["zone_id"] == 46
    cmp = metrics.rival_comparison(conn, "the-venomous-abyss", 5)
    names = [g["name"] for g in cmp["guilds"]]
    assert names[0] == "Killing Time" and "Internet Diff" in names and "Advance" in names
    kt = cmp["guilds"][0]
    assert kt["killed"] == 1 and kt["realm_rank"] == 3 and kt["world_rank"] == 2500
    assert kt["bosses"][1]["best_pct"] == 38.0 and kt["bosses"][1]["pulls"] == 4
    idiff = next(g for g in cmp["guilds"] if g["name"] == "Internet Diff")
    assert idiff["killed"] == 3 and idiff["pulls"] == 47 and idiff["bosses"][2]["first_kill"] == "2026-08-23"
    standings = metrics.realm_standings(conn, "the-venomous-abyss", 5)
    assert [s["name"] for s in standings] == ["Internet Diff", "Advance", "Killing Time"]
    assert standings[1]["current_prog"].startswith("ulatek 45.5")
    race = metrics.race_timeline(conn, "the-venomous-abyss", 5)
    assert race[0]["is_home"] and race[0]["points"] == [{"date": "2026-08-30", "kills": 1}]
    ov = metrics.overview(conn)
    assert ov["current_tier"]["id"] == 46
    assert ov["raiderio"][0]["raid_slug"] == "the-venomous-abyss" and ov["raiderio"][0]["heroic_world"] == 1904
